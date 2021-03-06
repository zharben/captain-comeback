# coding:utf-8
import os
import signal
import logging
import threading
import subprocess
import time
import errno

import psutil

from captain_comeback.restart.messages import (RestartRequestedMessage,
                                               RestartCompleteMessage)
from captain_comeback.activity.messages import (RestartCgroupMessage,
                                                RestartTimeoutMessage)


logger = logging.getLogger()


RESTART_STATE_POLLS = 20


class RestartEngine(object):
    def __init__(self, grace_period, job_queue, activity_queue):
        self.grace_period = grace_period
        self.job_queue = job_queue
        self.activity_queue = activity_queue
        self._counter = 0
        self._running_restarts = set()

    def _handle_restart_requested(self, cg):
        if cg in self._running_restarts:
            logger.info("%s: already being restarted", cg.name())
            return
        logger.debug("%s: scheduling restart", cg.name())
        self._running_restarts.add(cg)

        job_name = "restart-job-{0}".format(self._counter)
        self._counter += 1
        args = self.grace_period, cg, self.job_queue, self.activity_queue
        threading.Thread(target=restart, name=job_name, args=args).start()

    def _handle_restart_complete(self, cg):
        logger.debug("%s: registering restart complete", cg.name())
        self._running_restarts.remove(cg)

    def run(self):
        # TODO: Exit everything when this fails
        logger.info("ready to restart containers")
        while True:
            message = self.job_queue.get()
            if isinstance(message, RestartRequestedMessage):
                self._handle_restart_requested(message.cg)
            elif isinstance(message, RestartCompleteMessage):
                self._handle_restart_complete(message.cg)
            else:
                raise Exception("Unexpected message: {0}".format(message))


def restart(grace_period, cg, job_queue, activity_queue):
    # Snapshot task usage
    logger.info("%s: restarting", cg.name())

    activity_queue.put(RestartCgroupMessage(cg, cg.ps_table()))

    # We initiate the restart first. This increases our chances of getting a
    # successful restart by signalling a potential memory hog before we
    # allocate extra memory.

    # Our restart is two steps:
    # - First, we'll signal everyone in the cgroup to exit
    # - Second, we'll ask Docker to restart the container.
    # It's possible that between the two steps, the container will have
    # exited already, but Docker will do the right thing and restart our
    # process in this case.
    for pid in cg.pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            if e.errno == errno.ESRCH:
                # That process exited already. Who cares? We don't.
                logger.debug("%s: %s had already exited", cg.name(), pid)
            else:
                logger.error("%s: failed to deliver SIGTERM to %s: %s",
                             cg.name(), pid, e)

    signaled_at = time.time()

    # All that follows is optimistic. In practice, the container could
    # exit immediately and none of that would get a chance to run.

    try:
        # Try and allocate 10% of extra memory to give this cgroup a chance to
        # shut down gracefully. Note that we look at free memory (rather than
        # available) so that we don't have to e.g. free some buffers to grant
        # this extra memory.
        memory_limit = cg.memory_limit_in_bytes()
        free_memory = psutil.virtual_memory().free
        extra = int(memory_limit / 10)  # Make parameterizable

        logger.debug("%s: memory_limit: %s, free_memory: %s, extra: %s",
                     cg.name(), memory_limit, free_memory, extra)
        if free_memory > extra:
            new_limit = memory_limit + extra
            logger.info("%s: increasing memory limit to %s", cg.name(),
                        new_limit)
            cg.set_memory_limit_in_bytes(new_limit)

        # Now, we give grace_period to the container to exit. We'd like to use
        # docker restart -t for this, but unfortunately that does not work
        # reliably: https://github.com/docker/docker/issues/12738
        while time.time() < signaled_at + grace_period:
            time.sleep(float(grace_period) / RESTART_STATE_POLLS)
            try:
                pids = cg.pids()
            except EnvironmentError:
                # The cgroup is gone!
                logger.debug("%s: cgroup has exited after SIGTERM ", cg.name())
                break
            else:
                logger.debug("%s: Waiting for processes to exit: %s...",
                             cg.name(), ", ".join(str(p) for p in pids))
        else:
            logger.warning(
                "%s: container did not exit within %s seconds grace period",
                cg.name(), grace_period)
            activity_queue.put(RestartTimeoutMessage(cg, grace_period))
    except EnvironmentError:
        # This could happen if e.g. attempting to write to the memory limit
        # file after the cgroup has exited.
        pass
    finally:
        restart_cmd = ["docker", "restart", "-t", "0", cg.name()]
        proc = subprocess.Popen(restart_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

        out, err = proc.communicate()
        ret = proc.poll()
        if ret != 0:
            logger.error("%s: failed to restart", cg.name())
            logger.error("%s: status: %s", cg.name(), ret)
            logger.error("%s: stdout: %s", cg.name(), out)
            logger.error("%s: stderr: %s", cg.name(), err)

        logger.info("%s: restart complete", cg.name())
        job_queue.put(RestartCompleteMessage(cg))
