from datetime import datetime
from yeone_ocr.output_names import next_document_path,latest_document_path


def test_second_precision_names_keep_original_and_follow_date(tmp_path):
    pdf='중의역대약론선 (최적화, 인식) - 240203.pdf'
    first=next_document_path(tmp_path,pdf,'.hwpx',now=datetime(2026,11,17,9))
    assert first.name=='중의역대약론선 (최적화, 인식) - 240203 - 261117_090000.hwpx'
    first.write_bytes(b'old history')
    second=next_document_path(tmp_path,pdf,'.pdf',now=datetime(2026,11,17,15))
    assert second.name.endswith(' - 261117_150000.pdf');second.write_bytes(b'PDF')
    third=next_document_path(tmp_path,pdf,'.hwpx',now=datetime(2026,11,17,16))
    assert third.name.endswith(' - 261117_160000.hwpx')
    assert next_document_path(tmp_path,pdf,'.hwpx',now=datetime(2026,11,17,9)).name.endswith(' - 261117_090001.hwpx')
    assert first.read_bytes()==b'old history'
    assert next_document_path(tmp_path,pdf,'.hwpx',now=datetime(2026,11,18,0)).name.endswith(' - 261118_000000.hwpx')
    assert latest_document_path(tmp_path,pdf,'.pdf')==second


def test_old_outputs_and_split_outputs_remain_discoverable(tmp_path):
    old=tmp_path/'책_글자층.pdf';old.write_bytes(b'old')
    assert latest_document_path(tmp_path,'책.pdf','.pdf')==old
    (tmp_path/'책 - 20261117_09 - v3_01.hwpx').write_bytes(b'part1')
    assert next_document_path(tmp_path,'책.pdf','.hwpx',now=datetime(2026,11,17,10)).name=='책 - 261117_100000.hwpx'
    assert latest_document_path(tmp_path,'책.pdf','.hwpx').name=='책 - 20261117_09 - v3_01.hwpx'
    (tmp_path/'책 - 261117_100000_01.hwpx').write_bytes(b'part1')
    assert next_document_path(tmp_path,'책.pdf','.pdf',now=datetime(2026,11,17,10)).name=='책 - 261117_100001.pdf'
