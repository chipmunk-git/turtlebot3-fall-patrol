# 낙상 의심 감지 순찰 로봇 — 백엔드/프론트 작업 가이드

## 프로젝트

- 국비 팀프로젝트 (5명, 3주)
- 요양시설 같은 실내를 가정한다. TurtleBot3가 순찰하다가 낙상이 의심되는 사람을 발견하면 접근해서 관찰하고, 결과를 서버에 기록한다. 관리자는 웹에서 확인하고 처리한다
- 인식(YOLO Pose)은 PC에서 추론한다. 터틀봇은 영상 전송과 이동만 한다
- 내 역할은 DB·백엔드·웹 GUI다. 로봇 파트, 인식 파트와는 HTTP API로만 연결한다
- 화면과 문서에서는 "낙상"이 아니라 "낙상 의심"이라고 쓴다. 의료적 진단과 구분하기 위해서다

## 구성

```
터틀봇 --영상--> PC: YOLO / 로봇 제어 노드 --HTTP--> FastAPI --> SQLite
                                                       |
                                                       +--> 웹 대시보드 (같은 서버가 서빙, 폴링)
```

- 서버에 ROS 의존성을 넣지 않는다. ROS 노드가 HTTP로 서버를 호출한다
- 실시간 영상은 PC의 `web_video_server` 주소를 프론트가 직접 임베드한다. 서버는 관여하지 않는다
  - 인식 파트가 검출 결과(스켈레톤, 박스)를 그린 이미지를 토픽으로 발행하면 그 토픽을 보여준다. 그 전까지는 원본 카메라 토픽을 쓴다
  - 영상 주소는 "지금 접속한 호스트의 8080 포트"로 계산한다
- 지도는 SLAM으로 미리 만든 지도 이미지를 띄우고, 그 위에 로봇 위치와 시야각(부채꼴), 이벤트 위치(빨간 점)를 표시한다. 폴링하는 `/status`, `/events` 데이터를 그대로 쓴다
  - 시야각은 yaw 방향으로 카메라 화각(기본 62°)만큼, 인식 거리(기본 3m)만큼 그린다. 두 값은 프론트 설정값이다
- 갱신은 폴링으로 한다
  - 로봇 위치 `/status`: 0.2~0.3초. 로봇도 0.2초마다 PUT한다. 화면에서는 애니메이션으로 부드럽게 이동시킨다
  - 이벤트 `/events`: 2~3초
- 프론트는 HTML/JS 정적 파일이고 FastAPI가 같이 서빙한다. 서버가 하나뿐이고 빌드 도구가 필요 없다

## 감지 흐름

```
1. 순찰 중 누운 사람 발견 (좌표 x, y)      사진 ① 의심을 찍어서 들고 있는다
2. GET /ignore-zones/check?x=&y=        "이미 처리 중인 위치인가?"
3. ignored: true  → 사진 ①을 버리고 순찰 계속. 아무것도 저장하지 않는다
   ignored: false → 접근
4. 도착해서 멈춤                          사진 ② 관찰 시작
   POST /events (status=observing, 사진 ①②)   이 시점부터 이 위치는 무시 구역
5. 관찰 종료                              사진 ③ 판단
   POST /events/{id}/snapshots (kind=decision, 사진 ③)
   PATCH /events/{id} status=confirmed | uncertain | false_alarm
6. 순찰 재개
```

- 서버가 응답하지 않으면 로봇은 무시하지 않고 접근한다. 놓치는 것보다 한 번 더 확인하는 쪽이 안전하다
- 관찰 시간과 판단 기준은 로봇·인식 파트가 정한다. 서버는 결과를 기록만 한다

## 이벤트 상태

```
observing ──┬── confirmed ──── acknowledged ──── resolved
            ├── uncertain ──── (관리자가 confirmed 또는 false_alarm으로 변경)
            └── false_alarm
```

| 상태 | 의미 | 바꾸는 쪽 |
|---|---|---|
| observing | 접근해서 관찰 중 | 로봇 |
| confirmed | 낙상 의심 | 로봇 |
| uncertain | 판단 불가 (가림, 일부만 보임 등) | 로봇 |
| false_alarm | 오탐 | 로봇 또는 관리자 |
| acknowledged | 관리자가 확인함 | 관리자 (웹) |
| resolved | 처리 완료 | 관리자 (웹) |

- 대시보드 알림 배너는 `confirmed`, `uncertain` 상태인 이벤트가 있으면 표시한다. 관리자가 확인하면 사라진다
- 상태 전이를 서버에서 엄격하게 막지는 않는다. 값이 enum에 속하는지만 검사한다

## 무시 구역 판정

무시 구역은 별도 테이블 없이 이벤트를 보고 그때그때 계산한다.

`GET /ignore-zones/check?x=&y=`는 아래 조건을 모두 만족하는 이벤트가 하나라도 있으면 `{ "ignored": true }`를 돌려준다.

- 좌표가 있다 (x, y가 null이 아님)
- 요청 좌표와의 거리가 `IGNORE_RADIUS` 이하다
- 상태와 생성 시각이 아래 중 하나에 해당한다
  - `observing`, `confirmed`, `uncertain`, `acknowledged`이고 `IGNORE_TTL` 이내
  - `false_alarm`이고 `FALSE_ALARM_TTL` 이내

| 설정 | 기본값 |
|---|---|
| IGNORE_RADIUS | 1.0 m |
| IGNORE_TTL | 10분 |
| FALSE_ALARM_TTL | 3분 |

- 관리자가 `resolved`로 바꾸거나 TTL이 지나면 자동으로 판정에서 빠진다. 이벤트 자체는 이력으로 남는다
- `false_alarm`을 짧게 포함하는 이유는 누워 쉬는 사람이나 마네킹 앞에서 계속 멈추는 것을 막기 위해서다

## 기술 스택

- Python, FastAPI, SQLAlchemy 2.x, SQLite
- 패키지: fastapi, uvicorn[standard], sqlalchemy, python-multipart, pyyaml
- 프론트: HTML, CSS, JavaScript (프레임워크 없음)

## 디렉터리 구조

```
backend/
  app/
    main.py
    config.py
    database.py
    models.py
    schemas.py
    activity.py
    routers/
      events.py
      ignore_zones.py
      status.py
      logs.py
      map.py
  static/
    index.html
    app.js
    style.css
  maps/
    map.png
    map.yaml
    patrol.json        가짜 로봇 전용: 순찰 경로, 사람 위치, 감지 거리
  scripts/
    seed.py
    fake_robot.py
    fake_world.py
    convert_map.py
    map_cleaner.py
    patrol.py
    png_utils.py
  data/
    app.db
    snapshots/
  requirements.txt
```

## 데이터 모델

**events**

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | int | PK |
| created_at | datetime | UTC |
| updated_at | datetime | UTC |
| x, y | float, nullable | map 프레임, 미터. 좌표 방식이 정해지기 전까지 null 허용 |
| confidence | float, nullable | 인식 신뢰도 |
| status | enum | 위 상태표 |
| note | str, nullable | 관리자 메모 |
| acknowledged_at | datetime, nullable | acknowledged가 될 때 서버가 기록 |
| resolved_at | datetime, nullable | resolved가 될 때 서버가 기록 |

**event_snapshots** (이벤트 하나에 여러 장)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | int | PK |
| event_id | int | events.id |
| kind | enum | detected(① 의심), observing(② 관찰 시작), decision(③ 판단) |
| path | str | `data/snapshots/` 안의 파일 이름 |
| created_at | datetime | UTC |

이벤트의 대표 사진(`snapshot_url`)은 decision → observing → detected 순서로 있는 것을 쓴다.

**activity_logs** (서버가 API 호출을 받을 때 자동으로 기록)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | int | PK, 증가 순서 = 시간 순서 |
| created_at | datetime | UTC |
| kind | str | robot_state, ignore_check, event_created, event_snapshot, event_status, event_note |
| event_id | int, nullable | 관련 이벤트. 이벤트별 처리 이력 조회에 쓴다 |
| detail | JSON | 종류별 내용 (이전·이후 상태, 좌표, 사진 종류, 메모 등) |

**robot_status** (단일 행, id=1)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| state | enum | idle, patrol, suspect, approach, observe, alert |
| x, y | float, nullable | |
| yaw | float, nullable | 라디안. 지도에 방향 화살표로 표시 |
| current_waypoint | int, nullable | |
| updated_at | datetime | UTC |

상태값은 enum으로 한 곳(`models.py`)에서만 정의한다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health` | |
| POST | `/events` | multipart/form-data: x, y, confidence, status(기본 observing), detected_snapshot(① 파일, 선택), observing_snapshot(② 파일, 선택) |
| POST | `/events/{id}/snapshots` | multipart/form-data: kind(detected, observing, decision), snapshot(파일) |
| GET | `/events` | 쿼리: status(여러 개 가능), limit, offset. 최신순 |
| GET | `/events/{id}` | |
| PATCH | `/events/{id}` | JSON: status, note. 결과 업로드와 관리자 처리에 모두 쓴다 |
| GET | `/ignore-zones/check` | 쿼리: x, y → `{ "ignored": bool, "event_id": int \| null }` |
| GET | `/status` | 로봇 상태 |
| PUT | `/status` | 로봇이 주기적으로 갱신 (0.2초) |
| GET | `/logs` | 쿼리: after, limit, event_id → `{ last_id, entries }`. DB(activity_logs)에 저장된 활동 로그. event_id를 주면 그 이벤트의 처리 이력만 준다 |
| GET | `/map` | 지도 정보: image_url, resolution, origin_x, origin_y. 이미지 크기는 프론트가 이미지를 불러와서 얻는다 |

- 지도 파일은 `maps/`에 둔다. 브라우저는 pgm을 못 띄우므로 png로 변환해서 둔다. 실제 지도를 받기 전까지는 가짜 지도를 쓴다
- 좌표 → 픽셀: `px = (x - origin_x) / resolution`, `py = height - (y - origin_y) / resolution`
- 스냅샷은 `/snapshots/...` 정적 경로로 서빙하고, 이벤트 응답에 `snapshot_url`을 포함한다
- 대시보드는 `/`에서 서빙한다
- CORS는 개발 중 전체 허용한다

## 규칙

- 좌표는 map 프레임 기준 미터 단위다. 로봇 파트와 같은 값을 쓴다
- 시간은 UTC로 저장하고 ISO 8601(`Z` 포함)로 응답한다. SQLite는 타임존을 저장하지 않으므로 응답할 때 붙여준다. KST 변환은 프론트에서 한다
- 오류는 `HTTPException`으로 처리한다
- 코드는 깔끔하게, 타입힌트를 쓰고, 주석은 최소화한다
- 서버는 `--host 0.0.0.0`으로 실행해 다른 기기에서 접근할 수 있게 한다
- 테이블은 `create_all`로 만든다. 모델을 바꾸면 `data/app.db`를 지우고 다시 만든다

## 작업 순서

1. 프로젝트 세팅 (가상환경, requirements.txt)
2. config.py, database.py, models.py, schemas.py
3. events 라우터와 스냅샷 저장
4. ignore_zones(check), status, map 라우터
5. `scripts/seed.py` — 여러 상태의 더미 이벤트 20건과 샘플 이미지 생성. 지도가 없으면 가짜 지도도 만든다
6. `scripts/fake_robot.py` — 위 감지 흐름을 그대로 흉내 낸다
   - 가짜 지도의 웨이포인트를 순찰하고, 정해진 사람 위치에 가까워지면 check → 접근 → observing 생성 → 관찰 → 결과 변경
   - 로봇 상태를 0.2초마다 PUT한다
   - 표준 라이브러리만 써서 패키지 설치 없이 돌아간다
7. 실행 (아래 "실행 방법")
8. `/docs`에서 전체 엔드포인트 확인
9. 대시보드
   - 알림 배너 (confirmed, uncertain)와 확인 버튼
   - 이벤트 목록과 상세 (스냅샷, 상태 변경, 메모)
   - 로봇 상태
   - 실시간 영상 (web_video_server 임베드, YOLO 결과 토픽)
   - 지도 (로봇 위치·방향, 이벤트 위치)
   - 실시간 로그 (`/logs`)

## 실행 방법

`backend/`에서 실행한다.

```
# 처음 한 번 (윈도우는 py -3.10 -m venv .venv, .venv\Scripts\activate)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m scripts.seed                                   # 더미 데이터 (기존 이벤트 삭제됨)
python -m scripts.fake_world                             # 가짜 지도만 다시 만들기
python -m scripts.convert_map <경로>/map.yaml --clean    # 실제 SLAM 지도를 정리해서 maps/로 변환
                                                         # --clean: 방 밖 노이즈 제거, 벽 틈 메우기, 벽 두께 통일, 여백 자르기
                                                         # 해상도와 좌표계(origin)는 그대로 유지된다
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload # 서버
python -m scripts.fake_robot                             # 가짜 로봇 (다른 터미널)
python -m scripts.fake_robot --speed 2 --observe 2       # 빠르게 테스트
```

- 같은 PC에서 서버를 부를 때는 `localhost` 대신 `127.0.0.1`을 쓴다. 윈도우에서는 `localhost`가 IPv6를 먼저 시도해서 요청마다 2초씩 걸린다
- 대시보드는 브라우저에서 `http://localhost:8000` (다른 기기는 `http://<PC IP>:8000`)

## 리눅스 PC로 옮길 때 바꿀 곳

지금 코드는 노트북 테스트 기준이다. 옮길 때 아래만 바꾼다.

| 항목 | 위치 | 바꿀 내용 |
|---|---|---|
| 가상환경 | `backend/.venv` | 복사하지 않는다. 리눅스에서 `python3 -m venv .venv` 후 `pip install -r requirements.txt` |
| 데이터 | `backend/data/` | 복사하지 않는다. 서버를 처음 실행하면 빈 DB가 생긴다 |
| 지도 | `backend/maps/` | 로봇 파트의 SLAM 지도로 교체: `python -m scripts.convert_map <경로>/map.yaml --clean`. 지금은 실제 로봇으로 만든 미로 지도(`my-car-astar/maps/maze_map`)를 정리해서 넣어 두었다 |
| 가짜 로봇 경로 | `backend/maps/patrol.json` | 실제 로봇에는 필요 없다. 새 지도에서 가짜 로봇을 돌릴 때만 경로와 사람 위치를 그 지도에 맞게 고친다 |
| 영상 토픽 | `static/app.js` `CONFIG.videoTopic` | 실제 카메라 토픽, 또는 인식 파트의 결과 이미지 토픽 |
| 영상 포트 | `static/app.js` `CONFIG.videoPort` | web_video_server 포트가 8080이 아니면 변경 |
| 카메라 화각 | `static/app.js` `CONFIG.cameraFovDeg` | 실제 카메라 화각 (Pi Camera v2 약 62°) |
| 인식 거리 | `static/app.js` `CONFIG.viewRangeM` | 인식 파트 테스트 결과에 맞춰 조정 |
| 무시 구역 값 | `app/config.py` | 반경, TTL을 실제 테스트 후 조정 |
| 실행 명령 | - | `--reload` 빼고 실행: `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| 방화벽 | - | 다른 기기에서 접속하려면 `sudo ufw allow 8000` (ufw 사용 시) |
| 로봇 노드 | ROS 쪽 | 서버 주소 `http://127.0.0.1:8000`, `fake_robot.py`가 호출 예시 |

## 완료 기준

- `/docs`에서 모든 엔드포인트가 동작한다
- `fake_robot.py`를 돌리면 이벤트가 쌓이고, 같은 위치 근처는 무시되며, 대시보드에 표시된다
- 대시보드에서 확인·처리 완료를 누르면 상태가 바뀌고, resolved된 위치는 다시 감지된다
- 서버를 재시작해도 데이터가 유지된다

## 미정

- 이벤트 좌표를 구하는 방식. 정해지기 전까지 x, y는 null을 허용하고, null인 이벤트는 무시 구역 판정에서 빠진다

## 나중에

- 다른 파트에 공유할 API 명세 정리
- MCU 연동 (LED·부저·LCD 알림 ON/OFF 원격 설정)
- 실내 환경 센서 기록, RFID 현장 확인
- 텔레그램 푸시 알림
