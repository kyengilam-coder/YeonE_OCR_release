# YeonE_OCR 창 프로그램 실행기. 이 파일을 더블클릭하면 창이 뜹니다(Windows: pythonw 로 콘솔 없이 실행).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yeone_ocr.launcher import main
raise SystemExit(main())
