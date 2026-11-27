import html
import logging


_SEVERITIES = {
    "info": ("#38bdf8", 1.0, logging.INFO),
    "success": ("#a3e635", 1.0, logging.INFO),
    "warning": ("#fbbf24", 1.0, logging.WARNING),
    "alert": ("#f87171", 1.0, logging.ERROR),
    "error": ("#f87171", 1.0, logging.ERROR),
    "debug": ("#38bdf8", 0.7, logging.DEBUG),
}


def console_log(gcode, title, message="", severity="info"):
    """Log plain text and send a colored, HTML-escaped console message."""
    try:
        color, opacity, level = _SEVERITIES[severity]
    except KeyError:
        raise ValueError("Unknown console severity: %s" % severity)
    logging.log(
        level, "%s%s%s", title, ": " if title and message else "", message)
    style = "margin:0; opacity:%s; color:%s" % (opacity, color)
    heading = (
        '<p style="font-weight:bold; %s">%s</p>'
        % (style, html.escape(title).replace("\n", "<br>"))
    ) if title else ""
    body = (
        '<p style="%s">%s</p>'
        % (style, html.escape(message).replace("\n", "<br>"))
    ) if message else ""
    gcode.respond_raw("<div>%s%s</div>" % (heading, body))
