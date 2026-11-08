from collections import Counter
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'成本': 'Chi phí', '说人话': 'Nói dễ hiểu', '收益': 'Lợi ích', '证据等级': 'Mức độ bằng chứng', '来源': 'Nguồn', '备注': 'Lưu ý'}


def items(text):
    matches = list(re.finditer(r'^### (\d+)\. (.+)$', text, re.M))
    return {m.group(1): text[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)] for i, m in enumerate(matches)}


def urls(text):
    values = re.findall(r'https?://[^\s<>]+', text)
    return Counter(v.rstrip('。；，、；.,;）') for v in values)


def fields(text, name, chinese=False):
    delimiter = '：' if chinese else ':'
    return re.findall(r'^- ' + re.escape(name) + delimiter + r'\s*(.*)$', text, re.M)


def visible(text):
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'https?://[^\s<>]+', '', text)
    return text


def percentages(text):
    # Dấu phẩy thập phân tiếng Việt (83,4%), dấu chấm (83.4%), dấu chấm giữa
    # trong trích dẫn tiếng Anh (1·0%) và ký tự ％ toàn khổ đều là cùng một số.
    text = text.replace('％', '%').replace('·', '.')
    found = re.findall(r'[−-]?\d+(?:[.,]\d+)?\s*%', text)
    return Counter(re.sub(r'\s+', '', v).replace(',', '.') for v in found)


CHINESE_FRACTIONS = {'一': 10, '二': 20, '两': 20, '三': 30, '四': 40, '五': 50, '六': 60, '七': 70, '八': 80, '九': 90, '半': 5}
DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def numeral(text):
    # Đọc số Hán 0–999 trong 百分之… và …倍.
    total, current = 0, 0
    for char in text:
        if char == '百':
            total += (current or 1) * 100
            current = 0
        elif char == '十':
            total += (current or 1) * 10
            current = 0
        elif char in DIGITS:
            current = DIGITS[char]
    return total + current


def pct(value):
    # Tránh ký hiệu khoa học khi in JSON (0.001% thay vì 1e-05%).
    return ('%.6f' % value).rstrip('0').rstrip('.') + '%'


def fractions(text):
    # 七成 = 70%, 两成三 = 23%, 五成七 = 57%, 一成半 = 15%; 打七折 = 70%.
    found = Counter()
    for tens, ones in re.findall(r'([一二两三四五六七八九])成(半|[一二三四五六七八九])?', text):
        value = CHINESE_FRACTIONS[tens]
        if ones == '半':
            value += 5
        elif ones:
            value += DIGITS[ones]
        found[f'{value}%'] += 1
    found += Counter(f'{CHINESE_FRACTIONS[c]}%' for c in re.findall(r'打([一二三四五六七八九])折', text))
    found += Counter(pct(numeral(v)) for v in re.findall(r'百分之([零一二两三四五六七八九十百]+)', text))
    # 万分之五 = 0,05%; 十万分之一 = 0,001% (đừng đọc nhầm thành 万分之一).
    found += Counter(pct(numeral(v) / 100) for v in re.findall(r'(?<!十)万分之([一二三四五六七八九])', text))
    found += Counter(pct(numeral(v) / 100000) for v in re.findall(r'十万分之([一二三四五六七八九])', text))
    return found


VN_NUMBERS = {
    'một': 1, 'hai': 2, 'ba': 3, 'bốn': 4, 'tư': 4, 'năm': 5, 'sáu': 6, 'bảy': 7, 'tám': 8, 'chín': 9, 'mười': 10,
    'hai mươi': 20, 'ba mươi': 30, 'bốn mươi': 40, 'năm mươi': 50, 'sáu mươi': 60, 'bảy mươi': 70, 'tám mươi': 80, 'chín mươi': 90,
}


def spelled(text):
    # "ba phần trăm" là 3%, còn "hai phần mười" là 2/10 = 20%.
    units = '|'.join(sorted(VN_NUMBERS, key=len, reverse=True))
    found = re.findall(r'\b(' + units + r')\s+phần\s+(trăm|mười)\b', text, re.I)
    return Counter(f'{VN_NUMBERS[n.lower()] * (10 if u.lower() == "mười" else 1)}%' for n, u in found)


def data_fields(text, chinese):
    # Bỏ dòng "Nói dễ hiểu": bản gốc viết 两成 / 半成, bản dịch viết 20% / 5%,
    # nên phần trăm ở dòng đó lệch một cách hợp lệ. Các dòng dữ liệu còn lại phải khớp.
    name = '说人话' if chinese else 'Nói dễ hiểu'
    delimiter = '：' if chinese else ':'
    return re.sub(r'^- ' + re.escape(name) + delimiter + r'.*$', '', text, flags=re.M)


def number_tokens(text, chinese):
    body = visible(data_fields(text, chinese))
    tokens = percentages(body)
    tokens += fractions(body) if chinese else spelled(body)
    return tokens


def check():
    ap = argparse.ArgumentParser(description='Đối chiếu cấu trúc, nguồn và số liệu của bản dịch với bản gốc đã lưu.')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    metadata_path = ROOT / 'upstream.json'
    if not metadata_path.exists():
        raise SystemExit('Chưa có upstream.json; bản dịch chưa được ghép từ nguồn.')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    errors, warnings = [], []
    record_map = {r['id']: r for r in metadata['records']}
    total_items = 0
    expected_items = 0
    completed = 0
    for record in metadata['records']:
        key = record['id']
        source_path, target_path = ROOT / record['source'], ROOT / record['translation']
        expected_items += record['items']
        if not source_path.is_file():
            errors.append({'record': key, 'type': 'missing_source'})
            continue
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != record['source_sha256']:
            errors.append({'record': key, 'type': 'source_hash_changed'})
        if not target_path.is_file():
            errors.append({'record': key, 'type': 'missing_translation'})
            continue
        source = source_path.read_text(encoding='utf-8')
        target = target_path.read_text(encoding='utf-8')
        completed += 1
        a, b = items(source), items(target)
        total_items += len(b)
        if list(a) != list(b):
            errors.append({'record': key, 'type': 'item_numbers', 'source': list(a), 'translation': list(b)})
        for zh, vi in FIELDS.items():
            if len(fields(source, zh, True)) != len(fields(target, vi)):
                errors.append({'record': key, 'type': 'field_count', 'field': vi})
        a_grades = [v.strip() for v in fields(source, '证据等级', True)]
        b_grades = [v.strip() for v in fields(target, 'Mức độ bằng chứng')]
        # Chỉ so hạng A/B/C; chú thích đi kèm (争议) được dịch nên không so nguyên văn.
        if [re.match(r'[ABC]', v).group(0) if re.match(r'[ABC]', v) else v for v in a_grades] != \
           [re.match(r'[ABC]', v).group(0) if re.match(r'[ABC]', v) else v for v in b_grades]:
            errors.append({'record': key, 'type': 'evidence_grades'})
        if [('争议' in v) for v in a_grades] != [('tranh luận' in v) for v in b_grades]:
            errors.append({'record': key, 'type': 'dispute_marker'})
        if re.findall(r'<!--.*?-->', source, re.S) != re.findall(r'<!--.*?-->', target, re.S):
            errors.append({'record': key, 'type': 'metadata_comments'})
        pairs = [(n, a[n], b[n]) for n in a.keys() & b.keys()] if a else [('document', source, target)]
        for number, original, translation in pairs:
            for zh, vi in FIELDS.items():
                source_values = fields(original, zh, True)
                target_values = fields(translation, vi)
                if source_values and not target_values:
                    errors.append({'record': key, 'item': number, 'type': 'missing_field', 'field': vi})
            missing_urls = urls(original) - urls(translation)
            if missing_urls:
                errors.append({'record': key, 'item': number, 'type': 'missing_or_changed_urls', 'values': dict(missing_urls)})
            a_pct = number_tokens(original, True)
            b_pct = number_tokens(translation, False)
            if a_pct != b_pct:
                warnings.append({'record': key, 'item': number, 'type': 'percentages_need_review', 'source': dict(a_pct), 'translation': dict(b_pct)})
            for line_number, line in enumerate(translation.splitlines(), 1):
                # Dòng tài liệu tham khảo giữ nguyên tiếng Trung là đúng quy ước.
                if line.startswith('- Nguồn:') or line.startswith('Nguồn:') or 'http' in line:
                    continue
                line = visible(line)
                han = len(re.findall(r'[\u3400-\u9fff]', line))
                if han > 40 and han / max(len(line), 1) > 0.4:
                    errors.append({'record': key, 'item': number, 'type': 'untranslated_prose', 'line_in_item': line_number})
                # Chữ Hán dính liền chữ Latin không dấu cách là dấu hiệu còn sót
                # từ chưa dịch; tên riêng/điều luật hợp lệ luôn đứng trong ngoặc,
                # sách báo luôn có dấu cách hoặc 《》 bao quanh.
                for leftover in re.finditer(r'(?<![A-Z])[a-zà-ỹ][\u3400-\u9fff]|[\u3400-\u9fff](?![）》」』)\]])[a-zà-ỹ]', line):
                    errors.append({'record': key, 'item': number, 'type': 'stray_han', 'line_in_item': line_number, 'text': leftover.group(0)})
            for ch, item in re.findall(r'chương\s+(\d+)\s*[,，]\s*mục\s+(\d+)', translation, re.I):
                destination = record_map.get(ch.zfill(2))
                if not destination or not 1 <= int(item) <= destination['items']:
                    errors.append({'record': key, 'item': number, 'type': 'invalid_cross_reference', 'target': [ch, item]})
        for link in re.findall(r'\]\(([^)\s]+)\)', target):
            if re.match(r'^[a-zA-Z][\w+.-]*:', link):
                continue
            rel, _, fragment = unquote(link).partition('#')
            destination = (target_path.parent / rel).resolve() if rel else target_path
            if not destination.is_relative_to(ROOT.resolve()):
                errors.append({'record': key, 'type': 'link_escapes_repository', 'link': link})
            elif not destination.exists():
                errors.append({'record': key, 'type': 'broken_local_link', 'link': link})
            elif fragment.startswith('muc-') and destination.suffix == '.md':
                if f'id="{fragment}"' not in destination.read_text(encoding='utf-8'):
                    errors.append({'record': key, 'type': 'missing_anchor', 'link': link})
    result = {
        'source_commit': metadata['commit'],
        'expected_documents': len(metadata['records']), 'translated_documents': completed,
        'expected_items': expected_items, 'translated_items': total_items,
        'errors': errors, 'review_warnings': warnings,
        'note': 'Kiểm tra cơ học không chứng minh bản dịch hoàn toàn đúng nghĩa hoặc các khuyến nghị đúng chuyên môn.'
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f'Tài liệu: {completed}/{len(metadata["records"])}; mục: {total_items}/{expected_items}.')
        print(f'Lỗi: {len(errors)}; cảnh báo cần đối chiếu: {len(warnings)}.')
        for value in errors + warnings:
            print(json.dumps(value, ensure_ascii=False))
        print(result['note'])
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(check())
