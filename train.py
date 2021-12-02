import sys
from pathlib import Path

with open("/content/TTS/test_sentences.txt", mode="w") as f:
    f.write("""아래 문장들은 모델 학습을 위해 사용하지 않은 문장들입니다.
서울특별시 특허허가과 허가과장 허과장.
경찰청 철창살은 외철창살이고 검찰청 철창살은 쌍철창살이다.
지향을 지양으로 오기하는 일을 지양하는 언어 습관을 지향해야 한다.
그러니까 외계인이 우리 생각을 읽고 우리 생각을 우리가 다시 생각토록 해서 그 생각이 마치 우리가 생각한 것인 것처럼 속였다는 거냐?""")

cd /content/TTS
python TTS/bin/train_glow_tts.py \
     --continue_path "/content/data/glowtts-v2/glowtts-v2-December-01-2021_05+15AM-3aa165a")
