# 소멸위기에 처한 사투리 보전을 위한 제주어 오디오북, 코소롱
![](https://user-images.githubusercontent.com/81242672/169936577-7bc4d24a-53e0-4615-a019-9ef060b8946f.png)
## Project Description
- 사용자로부터 표준어 텍스트를 입력 받고 이를 제주어 오디오북으로 변환하는 웹 서비스
- 언어학적으로 중요한 의미를 지니는 제주어를 보전할 필요성을 알리고 제주어에 대한 흥미를 고취

## Developers
|[김민주](https://github.com/MINJU-KIMmm)|[이채은](https://github.com/lcheun)|[정수진](https://github.com/offsujin)|[진정현](https://github.com/jh-jin)|
|---|---|---|---|
|![minju](https://github.com/MINJU-KIMmm.png)|![cheun](https://github.com/lcheun.png)|![sj](https://github.com/offsujin.png)|![jh](https://github.com/jh-jin.png)|
|BackEnd, TTS, 팀장|FrontEnd, 기계번역|BackEnd, TTS|FrontEnd|

## Repository
### 🗂 toad-server
`Django`를 사용한 API 서버 Repository
- 책 조회, 생성, 삭제 기능
  - Python의 requests 라이브러리를 이용하여 딥러닝 서버와 연결
- 로그인 & 회원가입 기능
- 유저별 서재
- 좋아요를 이용한 북마크 기능
### 🗂 tts-server
`Flask`를 사용한 딥러닝 서버 Repository
- 음성합성 모델(Glow-TTS)와 기계번역 모델(Seq2Seq)을 이용한 오디오북 생성 기능
  - send_file을 이용하여 API 서버로 생성된 책 전달
### 🗂 Kongji-front
`React`를 사용한 프론트엔드 Repository
- API 서버와 API 통신하여 책 조회, 생성, 삭제
- 제주도의 특산물을 연상시키는 배색
### 🗂 kongji-translation
`Seq2Seq`와 `Attention`을 사용한 기계번역 Repository
- 표준어 텍스트 input -> 제주어 텍스트 output
- 15만개의 표준어-제주어 Dataset 사용
### 🗂 toad-glow-tts
`Glow-TTS`를 사용한 음성합성 Repository
- 텍스트를 제주 억양을 살려 발화하는 TTS
- 1만개의 제주어 단일 화자 오디오 Dataset 사용

## System Architecture
![sa](https://user-images.githubusercontent.com/81242672/169937276-3cf2821a-8fd7-44bc-8e0c-6083c7b18c86.png)
## Poster
![poster](https://user-images.githubusercontent.com/81242672/169936844-82d574c8-50ac-4b8e-aba4-35c99c1bdd33.png)


