from dataclasses import dataclass
from loguru import logger

# Every level method funnels through log_method, one frame below the plugin
# that called it, so loguru must read module/function/line one frame up or it
# would attribute every plugin record to this file. depth=1 does that from
# sys._getframe; the alternative, inspect.stack(), materializes the WHOLE
# stack and reads a source line for each frame -- per log call, on paths that
# run at the media tick rate. The instance is built once and shared: opt()
# carries only the options, and the core (sinks, patcher) stays shared by
# reference, so sinks added or reconfigured later still apply.
_CALLER_LOGGER = logger.opt(depth=1)

@dataclass
class Loglevel:
    name: str
    method_name: str
    priority: int
    color: str

@dataclass
class LoggerConfig:
    name: str

    log_file_path: str
    base_log_level: str
    rotation: str
    # Rotated files kept, oldest deleted first. A file sink without a
    # retention bound keeps every rotation forever, so this is not optional.
    retention: int
    compression: str

class Logger:
    def __init__(self, config: LoggerConfig, log_level: list[Loglevel]):
        self.name = config.name

        self.config = config
        self.log_level: dict[str, Loglevel] = {}
        self.sink_id: int | None = None

        for level in log_level:
            self.add_log_level(level)
            self.log_level[level.name] = level
        self.add_sink()

    def add_log_level(self, log_level: Loglevel):
        # Resolved once, not per call: the level name is fixed for the life of
        # this logger.
        level_name = f"{self.name}_{log_level.name}"
        logger.level(
            name=level_name,
            no=log_level.priority,
            color=f"{log_level.color}")

        def log_method(self, message, *args, **kwargs):
            _CALLER_LOGGER.log(level_name, message)

        setattr(self, log_level.method_name, log_method.__get__(self))

    def add_sink(self):
        # Prefix built once: the filter runs for every record this handler is
        # offered, not just the ones it accepts.
        level_prefix = f"{self.config.name}_"

        def log_filter(record):
            return record["level"].name.startswith(level_prefix)

        self.sink_id = logger.add(
            sink=self.config.log_file_path,
            level=self.config.base_log_level,
            rotation=self.config.rotation,
            retention=self.config.retention,
            compression=self.config.compression,
            enqueue=True,
            filter=log_filter,
            # {name} is the caller's module, which for a plugin under the data
            # directory is already the dotted path the old hand-built origin
            # string spelled out (plugins.<folder>.main).
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {function}:{line} - {message}"
        )

    def remove_sink(self):
        """Detach this sink and release its resources.

        The sink is added with ``enqueue=True``, so loguru backs it with a
        multiprocessing writer queue whose POSIX semaphores are only unlinked
        when the handler is removed. Callers on the quit path must invoke this
        BEFORE any ``os._exit`` (including the force_quit fallback), which would
        otherwise bypass loguru's cleanup and leave the queue's semaphores for
        the multiprocessing resource_tracker to report as leaked at shutdown.
        """
        if self.sink_id is not None:
            logger.remove(self.sink_id)
            self.sink_id = None

    def _log(self, level, message, *args, **kwargs):
        logger.log(level, message, *args, **kwargs)