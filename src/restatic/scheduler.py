import logging
from datetime import date, timedelta
from apscheduler.schedulers.qt import QtScheduler
from apscheduler.triggers import cron

from restatic.restic.create import ResticCreateThread
from .models import BackupProfileModel, EventLogModel
from restatic.restic.prune import ResticPruneThread
from restatic.restic.list import ResticListThread
from restatic.restic.check import ResticCheckThread
from .notifications import RestaticNotifications

logger = logging.getLogger("restatic")


class RestaticScheduler(QtScheduler):
    def __init__(self, parent):
        super().__init__()
        self.app = parent
        self.start()
        self.reload()

    def reload(self):
        for profile in BackupProfileModel.select():
            trigger = None
            job_id = f"{profile.id}"
            if profile.schedule_mode == "interval":
                trigger = cron.CronTrigger(
                    hour=f"*/{profile.schedule_interval_hours}",
                    minute=profile.schedule_interval_minutes,
                )
            elif profile.schedule_mode == "fixed":
                trigger = cron.CronTrigger(
                    hour=profile.schedule_fixed_hour,
                    minute=profile.schedule_fixed_minute,
                )
            if self.get_job(job_id) is not None and trigger is not None:
                self.reschedule_job(job_id, trigger=trigger)
                notifier = RestaticNotifications.pick()()
                notifier.deliver(
                    "Restatic Scheduler", "Background scheduler was changed."
                )
                logger.debug("Job for profile %s was rescheduled.", profile.name)
            elif trigger is not None:
                self.add_job(
                    func=self.create_backup,
                    args=[profile.id],
                    trigger=trigger,
                    id=job_id,
                    misfire_grace_time=180,
                )
                logger.debug("New job for profile %s was added.", profile.name)
            elif self.get_job(job_id) is not None and trigger is None:
                self.remove_job(job_id)
                logger.debug("Job for profile %s was removed.", profile.name)

    @property
    def next_job(self):
        self.wakeup()
        self._process_jobs()
        jobs = []
        for job in self.get_jobs():
            jobs.append((job.next_run_time, job.id))

        if jobs:
            jobs.sort(key=lambda job: job[0])
            profile = BackupProfileModel.get(id=int(jobs[0][1]))
            return f"{jobs[0][0].strftime('%H:%M')} ({profile.name})"
        else:
            return "None scheduled"

    def next_job_for_profile(self, profile_id):
        self.wakeup()
        job = self.get_job(str(profile_id))
        if job is None:
            return "None scheduled"
        else:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M")

    def create_backup(self, profile_id):
        notifier = RestaticNotifications.pick()()
        profile = BackupProfileModel.get(id=profile_id)
        logger.info("Starting background backup for %s", profile.name)
        msg = ResticCreateThread.prepare(profile)
        if msg["ok"]:
            logger.info("Preparation for backup successful.")
            thread = ResticCreateThread(msg["cmd"], msg)
            thread.start()
            thread.wait()
            if thread.process.returncode in [0, 1]:
                self.post_backup_tasks(profile_id)
            else:
                notifier.deliver("Restatic Backup", "Error during backup creation.")
                logger.error("Error during backup creation.")
        else:
            logger.error("Conditions for backup not met. Aborting.")
            logger.error(msg["message"])
            notifier.deliver("Restatic Backup", msg["message"])

    def post_backup_tasks(self, profile_id):
        """
        Pruning and checking after successful backup.
        """
        profile = BackupProfileModel.get(id=profile_id)
        logger.info("Doing post-backup jobs for %s", profile.name)
        if profile.prune_on:
            msg = ResticPruneThread.prepare(profile)
            if msg["ok"]:
                prune_thread = ResticPruneThread(msg["cmd"], msg)
                prune_thread.start()
                prune_thread.wait()

                # Refresh snapshots
                msg = ResticListThread.prepare(profile)
                if msg["ok"]:
                    list_thread = ResticListThread(msg["cmd"], msg)
                    list_thread.start()
                    list_thread.wait()

        validation_cutoff = date.today() - timedelta(days=7 * profile.validation_weeks)
        recent_validations = (
            EventLogModel.select()
            .where(
                (EventLogModel.subcommand == "check")
                & (EventLogModel.start_time > validation_cutoff)
                & (EventLogModel.repo_url == profile.repo.url)
            )
            .count()
        )
        if profile.validation_on and recent_validations == 0:
            msg = ResticCheckThread.prepare(profile)
            if msg["ok"]:
                check_thread = ResticCheckThread(msg["cmd"], msg)
                check_thread.start()
                check_thread.wait()

        logger.info("Finished background task for profile %s", profile.name)
