# coding:utf-8
import sys
import logging
import argparse
import threading
import time
from six.moves import queue

from captain_comeback.index import CgroupIndex
from captain_comeback.restart.engine import RestartEngine
from captain_comeback.activity.engine import ActivityEngine


logger = logging.getLogger()


DEFAULT_ROOT_CG = "/sys/fs/cgroup/memory/docker"
DEFAULT_ACTIVITY_DIR = "/var/log/container-activity"
DEFAULT_SYNC_TARGET_INTERVAL = 1
DEFAULT_RESTART_GRACE_PERIOD = 10


def main(root_cg_path, activity_path, sync_target_interval,
         restart_grace_period):
    threading.current_thread().name = "index"

    job_queue = queue.Queue()
    activity_queue = queue.Queue()
    index = CgroupIndex(root_cg_path, job_queue, activity_queue)
    index.open()

    restarter = RestartEngine(restart_grace_period, job_queue, activity_queue)
    restarter_thread = threading.Thread(target=restarter.run, name="restarter")
    restarter_thread.daemon = True
    restarter_thread.start()

    activity = ActivityEngine(activity_path, activity_queue)
    activity_thread = threading.Thread(target=activity.run, name="activity")
    activity_thread.daemon = True
    activity_thread.start()

    while True:
        index.sync()
        next_sync = time.time() + sync_target_interval
        while True:
            poll_timeout = next_sync - time.time()
            if poll_timeout <= 0:
                break
            logger.debug("poll with timeout: %s", poll_timeout)
            index.poll(poll_timeout)

        for thread in [activity_thread, restarter_thread]:
            if not thread.is_alive():
                logger.critical("thread %s is dead", thread.name)
                return 1

    return 0


def main_wrapper(args):
    desc = "Autorestart containers that exceed their memory allocation"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("--root-cg",
                        default=DEFAULT_ROOT_CG,
                        help="parent cgroup (children will be monitored)")
    parser.add_argument("--activity",
                        default=DEFAULT_ACTIVITY_DIR,
                        help="where to log activity")
    parser.add_argument("--sync-interval",
                        default=DEFAULT_SYNC_TARGET_INTERVAL, type=float,
                        help="target sync interval to refresh cgroups")
    parser.add_argument("--restart-grace-period",
                        default=DEFAULT_RESTART_GRACE_PERIOD, type=int,
                        help="how long to wait before sending SIGKILL")
    parser.add_argument("--debug", default=False, action='store_true',
                        help="enable debug logging")

    ns = parser.parse_args(args)

    log_level = logging.DEBUG if ns.debug else logging.INFO
    log_format = "%(asctime)-15s %(levelname)-8s %(threadName)-10s -- " \
                 "%(message)s"
    logging.basicConfig(level=log_level, format=log_format)
    logger.setLevel(log_level)

    sync_interval = ns.sync_interval
    if sync_interval < 0:
        logger.warning("invalid sync interval %s, must be > 0", sync_interval)
        sync_interval = DEFAULT_SYNC_TARGET_INTERVAL

    restart_grace_period = ns.restart_grace_period
    if restart_grace_period < 0:
        logger.warning("invalid restart grace period %s, must be > 0",
                       restart_grace_period)
        restart_grace_period = DEFAULT_RESTART_GRACE_PERIOD

    return main(ns.root_cg, ns.activity, sync_interval, restart_grace_period)


def cli_entrypoint():
    sys.exit(main_wrapper(sys.argv[1:]))


if __name__ == "__main__":
    cli_entrypoint()
