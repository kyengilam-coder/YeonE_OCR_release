"""Reusable, writable book-rule files; upgrades never replace user rules."""
import json
import os
from pathlib import Path
import tempfile
from .presets import DATA, BUILTIN, ALIASES, load_preset, resolve_preset, preset_choices, preset_label


def rules_dir():
    override = os.environ.get('YEONE_OCR_RULES_DIR')
    if override: return Path(override).expanduser().resolve()
    base = Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()/'.local'/'share'
    return base/'YeonE_OCR'/'book_rules'


def ensure_examples():
    directory = rules_dir(); directory.mkdir(parents=True, exist_ok=True)
    for source, name in (('book_rule_kaibao.json','개보본초.json'),('book_rule_yaolun.json','중의역대약론선.json')):
        try:
            with (directory/name).open('xb') as f: f.write((DATA/source).read_bytes())
        except FileExistsError: pass
    return directory


def library_choices(current=None):
    result = preset_choices(current); seen = {str(Path(ref).resolve()) for ref,_ in result if ref not in BUILTIN}
    directory = rules_dir()
    if directory.is_dir():
        for path in sorted(directory.glob('*.json')):
            if path.is_symlink() or str(path.resolve()) in seen: continue
            try: load_preset(path)
            except (ValueError, OSError): continue
            result.append((str(path.resolve()),preset_label(str(path.resolve()))))
    return result


def save_rule(ref, destination, *, template=None, label=None):
    """Validate then atomically save; relative template links follow the new file."""
    ref = ref or 'basic'
    preset = resolve_preset(ref)
    source = DATA/f'preset_{preset.id}.json' if ref in BUILTIN or ref in ALIASES else Path(ref)
    data = json.loads(source.read_text(encoding='utf-8-sig'))
    if preset.entry and data.get('entry'): data['entry']['when'] = preset.entry.when
    if template is not None: data['template'] = str(Path(template).resolve()) if template else ''
    elif preset.template: data['template'] = preset.template
    if label: data['label'] = label
    destination = Path(destination).expanduser().resolve()
    payload = json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=destination.parent,suffix='.json',delete=False) as f:
            f.write(payload); temporary=Path(f.name)
        load_preset(temporary)
        os.replace(temporary,destination)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    return destination
