import curses
import logging
import configparser
from taurus import Attribute

MAX_NAME_LENGTH = 8
SYMBOL_READONLY = "🚫"
SYMBOL_DISCONNECTED = "❌"
SYMBOL_MOVING = "▷"
SYMBOL_STOPPED = "✓"
SYMBOL_ULIMIT = "⤒"
SYMBOL_LLIMIT = "⤓"

log = logging.getLogger(__name__)


class TaurusMpgAxis:
    def __init__(self, name: str, taurusattribute: str, **kwargs):
        self.log = log
        self.attr = None
        self._stop_meth = None
        self.polling_period: int = 500
        self.increment_exponent: int = 0
        self.taurusattribute = taurusattribute
        self.name = name[:MAX_NAME_LENGTH]
        self.stop_meth = kwargs.get("stop_meth", "")
        self.format = kwargs.get("format", "14.3f")
        self.connect()

    def connect(self):
        try:
            self.attr = Attribute(self.taurusattribute)
            self.attr.read(cache=False)
            self.attr.activatePolling(self.polling_period)
            dev = self.attr.getParentObj()
            if self.stop_meth != "":
                self._stop_meth = getattr(dev, self.stop_meth)
        except Exception as e:
            self.log.error(e)
            self.attr = None

    def get_status_character(self) -> str:
        """
        Return a short (1-2 characters) status string for display.
        :return:
        """
        status = SYMBOL_DISCONNECTED
        try:
            self.attr.read(cache=False)
            if not self.attr.isWritable():
                status = SYMBOL_READONLY
            elif self.attr.rvalue != self.attr.wvalue:
                status = SYMBOL_MOVING
            else:
                status = SYMBOL_STOPPED
        except Exception as e:
            pass
        return status

    def get_limit_character(self) -> str:
        limit = " "
        try:
            if self.attr.rvalue == self.attr.getMinRange():
                limit = SYMBOL_LLIMIT
            elif self.attr.rvalue == self.attr.getMaxRange():
                limit = SYMBOL_ULIMIT
        except Exception as e:
            pass
        return limit

    def get_position_string(self) -> str:
        try:
            position = f"{float(self.attr.rvalue):{self.format}}"
        except Exception as e:
            display_width = int(self.format.split(".")[0])
            position = f"{'----':>{display_width}}"
        return position

    def increase_stepsize(self):
        if "1" in f"{10 ** (self.increment_exponent + 1):{self.format}}":
            self.increment_exponent += 1

    def decrease_stepsize(self):
        if "1" in f"{10 ** (self.increment_exponent - 1):{self.format}}":
            self.increment_exponent -= 1

    def get_stepsize(self) -> float:
        return 10 ** self.increment_exponent

    def get_stepsize_string(self) -> str:
        return f"{self.get_stepsize():{self.format}}"

    def move_step(self, direction: int) -> None:
        if direction not in [1, -1]:
            self.log.error(f"Direction needs to be ±1")
            return
        if not self.attr.isWritable():
            return

        try:
            target = self.attr.wvalue + direction * self.get_stepsize()
            if direction == 1:
                target = min(target, float(self.attr.getMaxRange()))
            else:
                target = max(target, float(self.attr.getMinRange()))
            self.attr.write(target)
        except Exception as e:
            self.log.error(f"Error moving {self.name}: {e}")

    def stop_move(self):
        try:
            if not self.attr.isWritable():
                return
            if callable(self._stop_meth):
                self._stop_meth()
            current_pos = self.attr.read(cache=False)
            self.attr.write(current_pos.rvalue)
        except Exception as e:
            self.log.error(f"Error stopping {self.name}: {e}")

    def get_string_tuple(self):
        status = self.get_status_character()
        limit = self.get_limit_character()
        position = self.get_position_string()
        return status, limit, position

    def __str__(self):
        status, limit, position = self.get_string_tuple()
        return f"{self.name}  {position}  {status}{limit}"


class TaurusMpg:

    def __init__(self, stdscr: curses.window, configfile: str):
        self.log = log
        self.axes = []
        self.active_axis = 0
        self._last_input = "NONE"
        self.config = configparser.ConfigParser()
        self.screen = stdscr
        curses.curs_set(0)  # invisible cursor
        curses.halfdelay(2)  # input timeout in 1/10 s
        self.load_config(configfile)
        self.initialize_axes()
        self.draw_window()

    def load_config(self, configfile):
        try:
            self.config.read(configfile)
            self.log.info(f"Read configuration from {configfile}")
        except Exception as e:
            self.log.error(f"Error reading configuration {configfile}: {e}")

        if "general" in self.config:
            general_settings = self.config.pop("general")
            self.log.info(general_settings)
            # TODO: parse, set up logging...

    def initialize_axes(self):
        self.axes = []
        for name in self.config.sections():
            axis_def = dict(self.config[name])
            taurusattribute = axis_def.pop("fqdn")
            self.axes.append(TaurusMpgAxis(name, taurusattribute, **axis_def))

    def draw_window(self):
        while True:
            self.screen.clear()
            for i, axis in enumerate(self.axes):
                line_format = curses.A_REVERSE if i == self.active_axis else curses.A_NORMAL
                status, limit, position = axis.get_string_tuple()
                line = f"{axis.name:>{MAX_NAME_LENGTH}s}  {position}  {status}{limit}"
                self.screen.addstr(i, 0, line, line_format)
                if i == self.active_axis:
                    idx = axis.get_stepsize_string().index("1")
                    self.screen.chgat(i, MAX_NAME_LENGTH + idx + 2, 1, line_format | curses.A_UNDERLINE)

            self.screen.addstr(
                len(self.axes) + 2, 0,
                f"DEBUG: {self._last_input}",
                )
            self.handle_input()

    def handle_input(self):
        ax = self.axes[self.active_axis]
        try:
            key = self.screen.getkey()
            if key in ["KEY_UP", "k"]:
                self.active_axis = (self.active_axis - 1) % len(self.axes)
            elif key in ["KEY_DOWN", "j"]:
                self.active_axis = (self.active_axis + 1) % len(self.axes)
            elif key in ["KEY_LEFT", "h"]:
                ax.increase_stepsize()
            elif key in ["KEY_RIGHT", "l"]:
                ax.decrease_stepsize()
            elif key == "+":
                ax.move_step(1)
            elif key == "-":
                ax.move_step(-1)
            elif key in ["^]", "s"]:
                ax.stop_move()
            self._last_input = key
        except curses.error:  # raised on input timeout (halfdelay mode)
            pass


if __name__ == "__main__":
    curses.wrapper(TaurusMpg, "config.ini")
