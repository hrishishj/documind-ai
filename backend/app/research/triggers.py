"""When should the assistant look at the web? Deliberately conservative: a wrong 'yes' costs a search credit and mixes
web text into an answer about the user's own documents (a 10-K is full of "search advertising", "current liabilities",
"online services", "recent accounting pronouncements"), so single words like "search", "news" or "current" never trigger.
"""
import re

# The user clearly asks for the web: the word web/internet/online/google combined with a search-ish phrase.
EXPLICIT = re.compile(
    r"\b(on|from|via|using|with|through|across) (the )?(web|internet|google)\b"
    r"|\b(web|internet|online|google) (search|research|sources?|results?)\b"
    r"|\b(search|research|look ?up|find|check|browse)\b[^.?!\n]{0,40}\b(on the web|online|on the internet|the web|the internet|google)\b"
    r"|\bgoogle\b|\bbrowse the\b|\b(search|research) (the )?(web|internet)\b", re.I)

# Freshness words. These only count when there are NO usable documents to answer from.
SOFT = re.compile(r"\b(latest|recent(ly)?|today|up[- ]to[- ]date|news|this (week|month|year)|right now|as of (now|today))\b", re.I)

# The user told us not to.
NEGATIVE = re.compile(
    r"\b(don'?t|do not|without|no need to|never)\b[^.?!\n]{0,30}\b(web|internet|search(ing)?|online)\b"
    r"|\b(only|just)\b[^.?!\n]{0,25}\b(my|the|these|this|uploaded)\b[^.?!\n]{0,15}\b(documents?|files?|docs?|deck|sheet|workbook|presentation|report)\b"
    r"|\bfrom (my|the|these) (documents?|files?|docs?)\b", re.I)


def wants_web(message: str, docs_ready: bool) -> bool:
    if NEGATIVE.search(message):
        return False
    if EXPLICIT.search(message):
        return True
    return bool(SOFT.search(message)) and not docs_ready
