from dataclasses import dataclass
from loguru import logger

# Every level method goes through log_method, one frame below the plugin that
# called it, so loguru must read module, function and line one frame up.
# depth=1 reads that frame with sys._getframe; inspect.stack() instead builds
# the full stack and reads a source line per frame, on paths that log at the
# media tick rate. One instance serves all of them. opt() copies the options only, and the
# core keeps its sinks and patcher by reference, so later sinks still apply.
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
    # How many rotated files to keep. loguru deletes the oldest first. A file
    # sink without this bound keeps every rotation forever.
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
        # Resolve this once rather than per call. The level name is fixed for
        # the life of this logger.
        level_name = f"{self.name}_{log_level.name}"
        logger.level(
            name=level_name,
            no=log_level.priority,
            color=f"{log_level.color}")

        def log_method(self, message, *args, **kwargs):
            _CALLER_LOGGER.log(level_name, message)

        setattr(self, log_level.method_name, log_method.__get__(self))

    def add_sink(self):
        # Build the prefix once. The filter runs for every record this handler
        # is offered, and not for the accepted ones alone.
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
            # {name} is the caller's module. For a plugin under the data
            # directory that is the dotted path plugins.<folder>.main.
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {function}:{line} - {message}"
        )

    def remove_sink(self):
        """Detach this sink and release its resources.

        add_sink passes enqueue=True, so loguru backs the sink with a
        multiprocessing queue and unlinks its POSIX semaphores only on removal.
        The quit path must call this before any os._exit, which skips loguru's
        cleanup and leaves the resource_tracker to report leaked semaphores.
        """
        if self.sink_id is not None:
            logger.remove(self.sink_id)
            self.sink_id = None

    def _log(self, level, message, *args, **kwargs):
        logger.log(level, message, *args, **kwargs)