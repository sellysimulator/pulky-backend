"""Wire enums (spec §2). Members are UPPER_SNAKE, values lowercase strings."""

from enum import StrEnum


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class Incoterm(StrEnum):
    EXW = "exw"
    CIP = "cip"
    DAP = "dap"
    DDP = "ddp"


class RoomState(StrEnum):
    LOBBY = "lobby"
    CONFIGURING = "configuring"
    RUNNING = "running"
    FINISHED = "finished"


class Phase(StrEnum):
    DECIDING = "deciding"
    FINISHED = "finished"


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


WEEKDAYS: tuple[Weekday, ...] = (
    Weekday.MON,
    Weekday.TUE,
    Weekday.WED,
    Weekday.THU,
    Weekday.FRI,
    Weekday.SAT,
    Weekday.SUN,
)
