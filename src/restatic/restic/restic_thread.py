import json
import os
import sys
import shutil
import signal
import logging
import subprocess
from PyQt5 import QtCore
from PyQt5.QtWidgets import QApplication
from subprocess import Popen, PIPE

from ..models import EventLogModel, BackupProfileMixin
from ..utils import keyring

mutex = QtCore.QMutex()
logger = logging.getLogger("restatic")


class ResticThread(QtCore.QThread, BackupProfileMixin):
    """
    Base class to run `restic` command line jobs. If a command needs more pre- or post-processing
    it should sublass `ResticThread`.
    """

    updated = QtCore.pyqtSignal(str)
    result = QtCore.pyqtSignal(dict)

    def __init__(self, cmd, params, parent=None):
        """
        Thread to run Restic operations in.

        :param cmd: Restic command line
        :param params: Pass options that were used to build cmd and may be needed to
                       process the result.
        :param parent: Parent window. Needs `thread.wait()` if none. (scheduler)
        """

        super().__init__(parent)
        self.app = QApplication.instance()
        self.app.backup_cancelled_event.connect(self.cancel)

        cmd[0] = self.prepare_bin()

        env = os.environ.copy()
        env["RESTIC_HOSTNAME_IS_UNIQUE"] = "1"
        if params.get("password") and params["password"] is not None:
            env["RESTIC_PASSWORD"] = params["password"]

        env["RESTIC_RSH"] = "ssh -oStrictHostKeyChecking=no"
        if params.get("ssh_key") and params["ssh_key"] is not None:
            env["RESTIC_RSH"] += f' -i ~/.ssh/{params["ssh_key"]}'

        self.env = env
        self.cmd = cmd
        self.params = params
        self.process = None

    @classmethod
    def is_running(cls):
        if mutex.tryLock():
            mutex.unlock()
            return False
        else:
            return True

    @classmethod
    def prepare(cls, profile):
        """
        Prepare for running Restic. This function in the base class should be called from all
        subclasses and calls that define their own `cmd`.

        The `prepare()` step does these things:
        - validate if all conditions to run command are met
        - build restic command

        `prepare()` is run 2x. First at the global level and then for each subcommand.

        :return: dict(ok: book, message: str)
        """
        ret = {"ok": False}

        # Do checks to see if running Restic is possible.
        if cls.is_running():
            ret["message"] = "Backup is already in progress."
            return ret

        if cls.prepare_bin() is None:
            ret["message"] = "Restic binary was not found."
            return ret

        if profile.repo is None:
            ret["message"] = "Add a backup repository first."
            return ret

        ret["ssh_key"] = profile.ssh_key
        ret["repo_id"] = profile.repo.id
        ret["repo_url"] = profile.repo.url
        ret["profile_name"] = profile.name
        ret["password"] = keyring.get_password(
            "restatic-repo", profile.repo.url
        )  # None if no password.
        ret["ok"] = True

        return ret

    @classmethod
    def prepare_bin(cls):
        """Find packaged restic binary. Prefer globally installed."""

        # Look in current PATH.
        if shutil.which("restic"):
            return "restic"
        else:
            # Look in pyinstaller package
            cwd = getattr(sys, "_MEIPASS", os.getcwd())
            meipass_restic = os.path.join(cwd, "bin", "restic")
            if os.path.isfile(meipass_restic):
                return meipass_restic
            else:
                return None

    def run(self):
        self.started_event()
        mutex.lock()
        log_entry = EventLogModel(
            category="restic-run",
            subcommand=self.cmd[1],
            profile=self.params.get("profile_name", None),
        )
        log_entry.save()

        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            preexec_fn = None
        except AttributeError:
            creationflags = 0
            preexec_fn = os.setsid

        self.process = Popen(
            self.cmd,
            stdout=PIPE,
            stderr=PIPE,
            bufsize=1,
            universal_newlines=True,
            env=self.env,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )

        for line in iter(self.process.stderr.readline, ""):
            try:
                self.process_line(line)  # hook for lines
                parsed = json.loads(line)
                if parsed["type"] == "log_message":
                    self.log_event(f'{parsed["levelname"]}: {parsed["message"]}')
                    level_int = getattr(logging, parsed["levelname"])
                    logger.log(level_int, parsed["message"])
                elif parsed["type"] == "file_status":
                    self.log_event(f'{parsed["path"]} ({parsed["status"]})')
            except json.decoder.JSONDecodeError:
                msg = line.strip()
                self.log_event(msg)
                logger.warning(msg)

        self.process.wait()
        stdout = self.process.stdout.read()
        result = {
            "params": self.params,
            "returncode": self.process.returncode,
            "cmd": self.cmd,
        }
        try:
            result["data"] = json.loads(stdout)
        except:  # noqa
            result["data"] = {}

        log_entry.returncode = self.process.returncode
        log_entry.repo_url = self.params.get("repo_url", None)
        log_entry.save()

        self.process_result(result)
        self.finished_event(result)
        mutex.unlock()

    def cancel(self):
        if self.isRunning():
            mutex.unlock()
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.terminate()

    def process_line(self, line):
        pass

    def process_result(self, result):
        pass

    def log_event(self, msg):
        self.updated.emit(msg)

    def started_event(self):
        self.updated.emit("Task started")

    def finished_event(self, result):
        self.result.emit(result)


class ResticThreadChain(ResticThread):
    """
    Metaclass of `ResticThread` that can run multiple other ResticThread actions while providing the same
    interface as a single action.
    """

    def __init__(self, cmds, input_values, parent=None):
        """
        Takes a list of tuples with `ResticThread` subclass and optional input parameters. Then all actions are executed
        and a merged result object is returned to the caller. If there is any error, then current result is returned.

        :param actions:
        :return: dict(results)
        """
        self.parent = parent
        self.threads = []
        self.combined_result = {}

        for cmd, input_value in zip(cmds, input_values):
            if input_value is not None:
                msg = cmd.prepare(input_value)
            else:
                msg = cmd.prepare()
            if msg["ok"]:
                thread = cmd(msg["cmd"], msg, parent)
                thread.updated.connect(
                    self.updated.emit
                )  # All log entries are immediately sent to the parent.
                thread.result.connect(self.partial_result)
                self.threads.append(thread)
        self.threads[0].start()

    def partial_result(self, result):
        if result["returncode"] == 0:
            self.combined_result.update(result)
            self.threads.pop(0)

            if len(self.threads) > 0:
                self.threads[0].start()
            else:
                self.result.emit(self.combined_result)
