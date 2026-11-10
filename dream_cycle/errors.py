"""Dream Cycle errors. Every message says what happened and what to do next."""


class DreamCycleError(Exception):
    pass


class DisabledError(DreamCycleError):
    pass


class LockBusy(DreamCycleError):
    def __init__(self, path: str):
        super().__init__(
            f"Another Dream Cycle run holds the lock ({path}). "
            "Wait for it to finish; if no run is active, the lock is released automatically when its process exits."
        )
        self.path = path


class ConfigError(DreamCycleError):
    pass
