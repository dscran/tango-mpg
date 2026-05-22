# Summary

Project to develop a hand-held, physical control for numeric tango variables.

Status: work in progress, loose collection of approaches


# Approaches

`main.py`: mock-up for keyboard-controlled interface and curses-based display. Uses taurus for tango interaction.
`config.ini`: draft of configuration file with axis definitions
`axis_serial.py`: Axis manager with serial interface. Intended as counterpart for esp32-based handheld controller.

## axis_serial.py

ASCII telegram exchange over a serial connection for axis control systems.

Outgoing telegram format (comma-delimited, newline-terminated) contains all data fields:
`<name>,<status>,<target_pos>,<current_pos>,<velocity>,<limit_status>\n`

Example:
`x_axis,idle,12.500,12.491,0.5,0\n`

Incoming telegram format (comma-delimited, newline-terminated) contains only axis name
and a single parameter/ value pair:
`<name>,<parameter>,<value>`

Valid parameters:
* `target`: new target position
* `velocity`: movement velocity
* `stop`: special parameter, any value stops movement on the given axis

Example:
* `Y_AXIS,target,-1.2\n`
* `ROT_Z,stop,1\n`
* `Z_AXIS,velocity,0.2\n`

Usage:
`python axis_serial.py --port /dev/ttyUSB0 --baud 115200`
