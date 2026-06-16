from django import template

register = template.Library()


@register.filter
def money(value):
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


@register.filter
def filesize(value):
    if value in (None, ""):
        return "-"
    try:
        size = int(value)
    except (TypeError, ValueError):
        return value
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
