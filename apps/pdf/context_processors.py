from django.utils.safestring import mark_safe
from .models import AdSnippet


def ad_snippets(request):
    """
    Context processor to inject active Adsterra and third-party ad snippets
    based on placement slot, page targeting, and device targeting.
    """
    path = request.path_info
    # Don't serve ads on admin or asset endpoints
    if path.startswith('/admin/') or path.startswith('/static/') or path.startswith('/media/') or path.startswith('/api/'):
        return {
            'ad_head_codes': [],
            'ad_top_codes': [],
            'ad_sidebar_codes': [],
            'ad_bottom_codes': [],
            'ad_footer_codes': [],
        }

    # Determine page classification
    is_home = path in ['/', '/km/', '/en/']
    is_policy = any(p in path for p in ['/privacy/', '/terms/', '/contact/'])
    is_tool = not is_home and not is_policy

    # Query active snippets ordered by display priority
    active_snippets = AdSnippet.objects.filter(is_active=True).order_by('order', '-created_at')

    ad_head_codes = []
    ad_top_codes = []
    ad_sidebar_codes = []
    ad_bottom_codes = []
    ad_footer_codes = []

    for item in active_snippets:
        # Evaluate page target filters
        if item.page_target == 'home_only' and not is_home:
            continue
        if item.page_target == 'tools_only' and not is_tool:
            continue
        if item.page_target == 'exclude_policy' and is_policy:
            continue

        snippet_data = {
            'id': item.id,
            'name': item.name,
            'code': mark_safe(item.code),
            'device_target': item.device_target,
        }

        if item.placement == 'head':
            ad_head_codes.append(snippet_data)
        elif item.placement == 'top_banner':
            ad_top_codes.append(snippet_data)
        elif item.placement == 'sidebar':
            ad_sidebar_codes.append(snippet_data)
        elif item.placement == 'bottom_banner':
            ad_bottom_codes.append(snippet_data)
        elif item.placement == 'footer_script':
            ad_footer_codes.append(snippet_data)

    return {
        'ad_head_codes': ad_head_codes,
        'ad_top_codes': ad_top_codes,
        'ad_sidebar_codes': ad_sidebar_codes,
        'ad_bottom_codes': ad_bottom_codes,
        'ad_footer_codes': ad_footer_codes,
    }
