import os
import struct
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def categorize_msgid(msgid):
    """Auto-categorize translation keys based on keywords."""
    low = msgid.lower()
    if any(k in low for k in ['organize', 'rotate', 'insert', 'reorder', 'drag pages', 'replace selected', 'add blank page']):
        return 'Organize'
    if any(k in low for k in ['compress', 'compression', 'dpi', 'smallest file size']):
        return 'Compress'
    if any(k in low for k in ['merge', 'combine them']):
        return 'Merge'
    if any(k in low for k in ['split', 'page range', 'page ranges']):
        return 'Split'
    if any(k in low for k in ['protect', 'password', 'lock pdf']):
        return 'Protect'
    if any(k in low for k in ['ocr', 'tesseract', 'searchable']):
        return 'OCR'
    if any(k in low for k in ['image to pdf', 'images to pdf', 'convert to pdf']):
        return 'ImageToPdf'
    if any(k in low for k in ['pdf to image']):
        return 'PdfToImage'
    if any(k in low for k in ['calendar', 'holiday', 'lunar', 'buddhist']):
        return 'Calendar'
    if any(k in low for k in ['tool', 'tools', 'menu', 'nav', 'home', 'contact']):
        return 'Tools'
    return 'General'


def unescape_po_string(s):
    """Unescape a quoted PO string line, preserving UTF-8 Unicode characters."""
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    res = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt == 'n':
                res.append('\n')
                i += 2
            elif nxt == 't':
                res.append('\t')
                i += 2
            elif nxt == 'r':
                res.append('\r')
                i += 2
            elif nxt == '"':
                res.append('"')
                i += 2
            elif nxt == '\\':
                res.append('\\')
                i += 2
            else:
                res.append(s[i:i + 2])
                i += 2
        else:
            res.append(s[i])
            i += 1
    return ''.join(res)


def escape_po_string(s):
    """Escape a string for writing into a .po file."""
    if s is None:
        return '""'
    escaped = (
        s.replace('\\', '\\\\')
         .replace('"', '\\"')
         .replace('\r', '\\r')
         .replace('\t', '\\t')
    )
    if '\n' in escaped:
        lines = escaped.split('\n')
        res = ['""']
        for idx, line in enumerate(lines):
            suffix = '\\n"' if idx < len(lines) - 1 else '"'
            res.append(f'"{line}{suffix}')
        return '\n'.join(res)
    return f'"{escaped}"'


def read_po_entries(po_path):
    """Parse a .po file and return list of (msgid, msgstr)."""
    if not os.path.exists(po_path):
        return []

    with open(po_path, 'r', encoding='utf-8') as f:
        content = f.read()

    entries = []
    lines = content.splitlines()
    msgid = None
    msgstr = None
    state = None

    def add_entry(m_id, m_str):
        if m_id is not None and m_str is not None:
            entries.append((m_id, m_str))

    for line in lines:
        sline = line.strip()
        if not sline or sline.startswith('#'):
            continue
        if sline.startswith('msgid '):
            add_entry(msgid, msgstr)
            msgid = unescape_po_string(sline[6:])
            msgstr = None
            state = 'msgid'
        elif sline.startswith('msgstr '):
            msgstr = unescape_po_string(sline[7:])
            state = 'msgstr'
        elif sline.startswith('"'):
            if state == 'msgid':
                msgid += unescape_po_string(sline)
            elif state == 'msgstr':
                msgstr += unescape_po_string(sline)

    add_entry(msgid, msgstr)
    return entries


def write_binary_mo(messages_dict, mo_path):
    """Write binary GNU MO file from a dictionary of {msgid: msgstr}."""
    # Filter out empty translations except header
    clean_messages = {}
    for k, v in messages_dict.items():
        if k == "" or (v and v.strip() != ""):
            clean_messages[k] = v

    keys = sorted(clean_messages.keys(), key=lambda x: x.encode('utf-8'))

    offsets = []
    ids = b''
    strs = b''

    for key in keys:
        val = clean_messages[key]
        key_bytes = key.encode('utf-8') + b'\x00'
        val_bytes = val.encode('utf-8') + b'\x00'
        offsets.append((len(ids), len(key_bytes) - 1, len(strs), len(val_bytes) - 1))
        ids += key_bytes
        strs += val_bytes

    keystart = 7 * 4
    valstart = keystart + len(keys) * 8
    keyoffsets = []
    valoffsets = []

    for o1, l1, o2, l2 in offsets:
        keyoffsets.append((l1, valstart + len(keys) * 8 + o1))
        valoffsets.append((l2, valstart + len(keys) * 8 + len(ids) + o2))

    output = bytearray()
    output += struct.pack('Iiiiiii', 0x950412de, 0, len(keys), keystart, valstart, 0, 0)
    for l, o in keyoffsets:
        output += struct.pack('ii', l, o)
    for l, o in valoffsets:
        output += struct.pack('ii', l, o)
    output += ids
    output += strs

    os.makedirs(os.path.dirname(mo_path), exist_ok=True)
    with open(mo_path, 'wb') as f:
        f.write(output)


def import_translations_from_po():
    """Import all strings from locale/km/LC_MESSAGES/django.po into database."""
    from .models import SiteTranslation

    po_path = os.path.join(settings.BASE_DIR, 'locale', 'km', 'LC_MESSAGES', 'django.po')
    entries = read_po_entries(po_path)

    created_count = 0
    updated_count = 0

    for msgid, msgstr in entries:
        if not msgid:
            continue  # Skip header
        cat = categorize_msgid(msgid)
        obj, created = SiteTranslation.objects.get_or_create(
            msgid=msgid,
            defaults={
                'msgstr': msgstr,
                'category': cat,
                'is_active': True,
            }
        )
        if created:
            created_count += 1
        elif msgstr and obj.msgstr != msgstr:
            obj.msgstr = msgstr
            obj.save(update_fields=['msgstr'])
            updated_count += 1

    return created_count, updated_count, len(entries)


def compile_translations_to_files():
    """Write database translations to PO and compile binary MO files."""
    from .models import SiteTranslation

    km_dir = os.path.join(settings.BASE_DIR, 'locale', 'km', 'LC_MESSAGES')
    en_dir = os.path.join(settings.BASE_DIR, 'locale', 'en', 'LC_MESSAGES')
    os.makedirs(km_dir, exist_ok=True)
    os.makedirs(en_dir, exist_ok=True)

    po_km_path = os.path.join(km_dir, 'django.po')
    mo_km_path = os.path.join(km_dir, 'django.mo')
    mo_en_path = os.path.join(en_dir, 'django.mo')

    # Read existing header from PO if available
    header_str = (
        'Content-Type: text/plain; charset=UTF-8\n'
        'Content-Transfer-Encoding: 8bit\n'
        'Language: km\n'
    )
    existing_entries = read_po_entries(po_km_path)
    for m_id, m_str in existing_entries:
        if m_id == "":
            header_str = m_str
            break

    # Build active translations dictionary
    translations = SiteTranslation.objects.filter(is_active=True)
    km_messages = {"": header_str}

    # Write PO file
    po_lines = [
        '# Khmer translation for DAMSPEY (Managed via Admin)',
        'msgid ""',
        f'msgstr {escape_po_string(header_str)}',
        '',
    ]

    for item in translations:
        km_messages[item.msgid] = item.msgstr
        po_lines.append(f'# Category: {item.category}')
        po_lines.append(f'msgid {escape_po_string(item.msgid)}')
        po_lines.append(f'msgstr {escape_po_string(item.msgstr)}')
        po_lines.append('')

    with open(po_km_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(po_lines))

    # Compile KM MO file
    write_binary_mo(km_messages, mo_km_path)

    # Compile clean EN MO file (header only, ensuring fallback works)
    en_header = (
        'Content-Type: text/plain; charset=UTF-8\n'
        'Content-Transfer-Encoding: 8bit\n'
        'Language: en\n'
    )
    write_binary_mo({"": en_header}, mo_en_path)

    # Clear Django internal translation cache
    try:
        from django.utils.translation import trans_real
        trans_real._translations = {}
        trans_real._default = None
    except Exception as e:
        logger.warning(f'Could not clear translation cache: {e}')

    return len(translations)
