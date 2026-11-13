#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Live ``workflowTuning`` configuration: enable, disable and relocate
tuning sample export without restarting LocalServer.

The ``WorkflowNodeOverrides`` poller (``config_overrides``) already reads
the whole LocalServer component configuration every 5 s; it hands every
good read to :meth:`TuningExportReconciler.on_configuration`. The
reconciler compares the raw ``workflowTuning`` section with the last one
it saw and, when the parsed :class:`ExportConfig` changed, applies one of
three transitions:

* **enable** (unconfigured -> valid): install a new exporter, start the
  marker-guarded backfill and a job runner whose first shadow read runs
  on the daemon thread ``workflow-tuning-job-sync``;
* **disable** (valid -> absent, disabled or malformed): uninstall the
  exporter, drop its queue and close the job runner, whose accepted jobs
  are reported ``failed``;
* **relocate** (valid A -> valid B): install the new exporter FIRST, move
  the old queue into it, close the runner and start a new one that
  inherits the handled job ids, and resume the backfill.

A transition never reads the tuning shadow on its own thread, and every
step is contained: a failure logs one WARNING and the remaining steps
still run.
"""

import copy
import logging
import threading
from typing import Any, Callable, Dict, Mapping, Optional

from workflow_engine.tuning.job_runner import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    job_runner,
)
from workflow_engine.tuning.sample_export import (
    CONFIG_KEY,
    ExportConfig,
    SampleExporter,
    set_sample_exporter,
)

logger = logging.getLogger(__name__)

#: The ``error`` every Device_Score_Job interrupted by a transition reports.
RECONFIGURED_REASON = "tuning export was reconfigured on the device"
#: The daemon thread that performs a new runner's first shadow read.
JOB_SYNC_THREAD_NAME = "workflow-tuning-job-sync"

_UNSEEN = object()


class TuningExportReconciler:
    """Applies ``workflowTuning`` changes read by the configuration poller.

    ``start_backfill(exporter, abort_if_replaced=True)`` starts the
    marker-guarded backfill on its own thread; ``start_job_runner(exporter,
    handled_jobs=None, sync=True)`` installs a runner and returns it (or
    ``None``); ``stop_job_runner(reason)`` uninstalls and closes the
    installed runner and returns its handled job ids. ``exporter_factory``
    builds an exporter from an :class:`ExportConfig`.
    """

    def __init__(
        self,
        start_backfill: Callable[..., Any],
        start_job_runner: Callable[..., Any],
        stop_job_runner: Callable[[str], Any],
        exporter_factory: Callable[[ExportConfig], Any] = SampleExporter,
        log: logging.Logger = logger,
    ) -> None:
        self._start_backfill = start_backfill
        self._start_job_runner = start_job_runner
        self._stop_job_runner = stop_job_runner
        self._exporter_factory = exporter_factory
        self._log = log
        self._lock = threading.Lock()
        #: The last raw ``workflowTuning`` section seen (a deep copy).
        self._raw: Any = _UNSEEN
        #: True once :meth:`adopt` recorded the startup state.
        self._adopted = False
        self._exporter: Any = None
        self._config: Optional[ExportConfig] = None

    # ------------------------------------------------------------------
    def adopt(self, exporter: Optional[SampleExporter]) -> None:
        """Record the exporter startup installed and start its backfill and
        job runner (the runner's first reconciliation runs here, on the
        startup thread, as before). ``None`` starts nothing."""
        with self._lock:
            self._adopted = True
            if exporter is None:
                return
            self._exporter = exporter
            self._config = getattr(exporter, "config", None)
            self._attempt("the tuning sample backfill could not start",
                          self._start_backfill, exporter,
                          abort_if_replaced=True)
            self._attempt("the tuning job runner could not start",
                          self._start_job_runner, exporter)

    def on_configuration(self, configuration: Any) -> None:
        """One good read of the component configuration (the poller's
        thread, or the startup thread for the first read). Never raises."""
        try:
            section = (configuration.get(CONFIG_KEY)
                       if isinstance(configuration, Mapping) else None)
            with self._lock:
                if self._raw is not _UNSEEN and section == self._raw:
                    return
                first = self._raw is _UNSEEN
                self._raw = copy.deepcopy(section)
                config = ExportConfig.from_section(section)
                if (config is None and section is not None
                        and not (first and self._adopted
                                 and self._config is None)):
                    # A startup that installed nothing already logged this
                    # WARNING for its section, so the first read does not
                    # repeat it; every later change to a bad section logs
                    # it once.
                    self._log.warning(
                        "Tuning sample export is disabled: the '%s' "
                        "component configuration is absent, disabled or "
                        "malformed (it needs enabled=true, a bucket, and a "
                        "prefix ending in '/')", CONFIG_KEY)
                if config == self._config:
                    return
                if self._exporter is None:
                    self._enable(config)
                elif config is None:
                    self._disable()
                else:
                    self._relocate(config)
        except Exception as error:  # noqa: BLE001 - the poller must go on
            self._log.warning(
                "Tuning configuration update could not be applied fully: %s",
                error, exc_info=True)

    # ------------------------------------------------------------------
    def _enable(self, config: ExportConfig) -> None:
        exporter = self._exporter_factory(config)
        set_sample_exporter(exporter)
        self._exporter, self._config = exporter, config
        self._log.info(
            "Tuning sample export enabled for device '%s': s3://%s/%s "
            "(configuration update, no restart)",
            getattr(exporter, "thing_name", ""), config.bucket, config.prefix)
        self._attempt("the tuning sample backfill could not start",
                      self._start_backfill, exporter, abort_if_replaced=True)
        runner = self._attempt("the tuning job runner could not start",
                               self._start_job_runner, exporter, sync=False)
        self._sync_later(runner)

    def _disable(self) -> None:
        old = self._exporter
        set_sample_exporter(None)
        self._exporter, self._config = None, None
        dropped = self._attempt("the old exporter could not be closed",
                                old.close_and_take) or []
        self._log.info(
            "Tuning sample export disabled by a configuration update; %d "
            "queued sample(s) dropped", len(dropped))
        self._attempt("the tuning job runner could not be stopped",
                      self._stop_jobs)

    def _relocate(self, config: ExportConfig) -> None:
        old, old_config = self._exporter, self._config
        new = self._exporter_factory(config)
        # Installed BEFORE the old one closes, so a sample that reaches the
        # closed exporter is forwarded here.
        set_sample_exporter(new)
        self._exporter, self._config = new, config
        moved = self._attempt("the old exporter could not be closed",
                              old.close_and_take) or []
        for sample in moved:
            new.enqueue(sample)
        self._log.info(
            "Tuning sample export moved from s3://%s/%s to s3://%s/%s by a "
            "configuration update; %d queued sample(s) moved",
            old_config.bucket, old_config.prefix, config.bucket,
            config.prefix, len(moved))
        handled = self._attempt("the tuning job runner could not be stopped",
                                self._stop_jobs) or {}
        runner = self._attempt("the tuning job runner could not start",
                               self._start_job_runner, new,
                               handled_jobs=handled, sync=False)
        self._sync_later(runner)
        self._attempt("the tuning sample backfill could not start",
                      self._start_backfill, new, abort_if_replaced=True)

    # ------------------------------------------------------------------
    def _stop_jobs(self) -> Dict[str, str]:
        """Close the installed runner; logs how many accepted, unfinished
        jobs it reports ``failed``."""
        runner = job_runner()
        before = runner.handled_jobs() if runner is not None else {}
        handled = self._stop_job_runner(RECONFIGURED_REASON) or {}
        failed = sum(1 for status in before.values()
                     if status in (STATUS_QUEUED, STATUS_RUNNING))
        if failed:
            self._log.info("%d tuning job(s) reported failed: %s", failed,
                           RECONFIGURED_REASON)
        return dict(handled)

    def _sync_later(self, runner: Any) -> None:
        """The new runner's first reconciliation, off the calling thread
        (a shadow read can wait for the 10 s IPC timeout)."""
        if runner is None:
            return
        self._attempt(
            "the tuning job runner's first shadow read could not start",
            threading.Thread(target=runner.on_delta, args=(None,),
                             name=JOB_SYNC_THREAD_NAME, daemon=True).start)

    def _attempt(self, what: str, function: Callable[..., Any], *args: Any,
                 **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - the next step still runs
            self._log.warning(
                "Tuning configuration update could not be applied fully: "
                "%s (%s)", what, error, exc_info=True)
            return None


__all__ = [
    "JOB_SYNC_THREAD_NAME",
    "RECONFIGURED_REASON",
    "TuningExportReconciler",
]
