"""Lightweight visitor tracking.

Logs one PageVisit row per real page view. Deliberately excludes
/admin/, /static/, /media/, /api/ (job polling, downloads) and non-GET
requests so the numbers reflect actual human page visits, not
background AJAX/polling traffic.
"""
import re

BOT_UA_RE = re.compile(
    r'bot|spider|crawl|slurp|bingpreview|facebookexternalhit|whatsapp|'
    r'telegrambot|discordbot|pingdom|uptimerobot|ahrefs|semrush|mj12bot',
    re.IGNORECASE,
)

EXCLUDED_PREFIXES = ('/admin', '/static', '/media', '/api', '/i18n')


def _get_client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class VisitorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        try:
            if (
                request.method == 'GET'
                and response.status_code < 400
                and not request.path.startswith(EXCLUDED_PREFIXES)
            ):
                self._log_visit(request)
        except Exception:
            # Tracking must never break the actual request.
            pass

        return response

    def _log_visit(self, request):
        from .models import PageVisit

        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
        PageVisit.objects.create(
            path=request.path[:255],
            ip_address=_get_client_ip(request),
            user_agent=user_agent,
            referrer=request.META.get('HTTP_REFERER', '')[:500],
            language=(request.LANGUAGE_CODE or '')[:10] if hasattr(request, 'LANGUAGE_CODE') else '',
            is_bot=bool(BOT_UA_RE.search(user_agent)),
        )
