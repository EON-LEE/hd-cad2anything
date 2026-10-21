# Atlas 웹앱 (atlas-web)

Atlas는 백화점 운영사를 위한 에이전트 기반 공간 분석 웹앱이다. 도면 캔버스, 에이전트 채팅, 개정 검토, 레이아웃 대안, 관계 그래프를 한 화면에서 제공한다.

에이전트(백엔드)는 화면을 직접 그리지 않는다. 에이전트는 채팅 응답 스트림에 `STATE_DELTA` 이벤트를 보내고, 웹앱은 이 이벤트로 UI 상태를 바꾼다. 계약은 [Atlas API 계약](../../docs/specs/atlas-api.md)을 따른다.

- **STATE_DELTA:** UI 상태에 적용할 JSON Patch(RFC 6902) 배열을 담은 이벤트다.
- **좌표:** 도면은 건물 남서쪽 모서리를 원점으로 하는 로컬 미터 좌표다. 지도 타일과 위경도를 쓰지 않는다.
- **데이터:** 모의 데이터의 브랜드명은 모두 가상이다.

## 1. 요구 사항

| 항목 | 버전 | 비고 |
|---|---|---|
| Node.js | 24 LTS(24.15 이상) 권장 | Vite 8은 20.19 이상 또는 22.12 이상이 필요하다. 테스트 도구(Vitest 5, jsdom 30)는 22.22.2 이상 또는 24.15 이상이 필요하다. |
| npm | Node.js에 포함된 버전 | 레지스트리 설정을 바꾸지 않는다. |
| 백엔드 | `atlas-api`(FastAPI) | 없으면 모의 모드로 실행된다. |

`package-lock.json`은 사용하는 레지스트리 주소를 기록하므로 Git에서 제외한다.

## 2. 실행

모든 명령은 `apps/atlas-web` 폴더에서 실행한다.

### 2.1 설치

```powershell
npm install
```

### 2.2 백엔드와 함께 실행

1. 백엔드 `atlas-api`를 `http://localhost:8000`에서 실행한다.
2. 개발 서버를 실행한다.

   ```powershell
   npm run dev
   ```

3. 브라우저에서 `http://localhost:5173`을 연다.

개발 서버는 `/api` 요청을 `http://localhost:8000`으로 전달한다. 백엔드 주소가 다르면 개발 서버를 실행하기 전에 `ATLAS_API_URL` 환경 변수를 지정한다.

```powershell
$env:ATLAS_API_URL = 'http://localhost:8010'; npm run dev
```

### 2.3 모의 모드로 실행

모의 모드는 백엔드 없이 `src/mock/`의 내장 데이터로 동작한다. 아래 방법 중 하나를 사용한다.

| 방법 | 명령 또는 동작 |
|---|---|
| npm 스크립트 | `npm run dev:mock` |
| 환경 변수 | `$env:VITE_MOCK = '1'; npm run dev` |
| 환경 파일 | `.env.example`을 `.env.local`로 복사하고 `VITE_MOCK=1`로 바꾼다. `.env.local`은 Git에서 제외된다. |
| URL 매개변수 | 주소 끝에 `?mock=1`을 붙인다. 빌드 결과물에서도 동작한다. |
| 자동 전환 | `GET /api/meta`가 실패하거나 8초 안에 응답하지 않으면 모의 모드로 전환한다. |

모의 모드에서는 상단에 `모의 데이터` 배지와 전환 사유가 표시된다.

### 2.4 빌드

```powershell
npm run build
```

- `tsc -b`로 타입을 검사한 뒤 `vite build`를 실행한다. 타입 오류가 있으면 빌드가 실패한다.
- 결과물은 `apps/atlas-web/dist`에 생성된다. 이 폴더는 Git에서 제외한다.
- FastAPI 백엔드가 `dist`를 `/` 경로에서 제공하는 것을 전제로 `base`를 `/`로 설정했다. 자산 경로는 `/assets/`이다.
- 작업 중인 백엔드 구현(`src/cad2anything/services/atlas_api.py`)은 `ATLAS_WEB_DIR` 환경 변수로 지정한 폴더를 `/`에 연결한다. 이 경우 `ATLAS_WEB_DIR`에 `apps/atlas-web/dist`의 경로를 지정한다.
- `npm run build:mock`은 모의 모드를 고정한 결과물을 만든다.
- `npm run preview`는 빌드 결과물을 로컬에서 확인한다. 이때도 `/api` 프록시가 동작한다.

### 2.5 테스트

```powershell
npm test
```

| 테스트 파일 | 확인 내용 |
|---|---|
| `src/lib/jsonPatch.test.ts` | JSON Patch 적용기: 객체·배열의 add/replace/remove, move/copy/test, 오류 시 원자적 실패, 입력 불변성 |
| `src/lib/sse.test.ts` | SSE 파서: 임의 위치에서 나뉜 청크, CRLF, 여러 줄 `data`, 한글 UTF-8 바이트 분할, 중단 |
| `src/state/reducer.test.ts` | 이벤트 순서대로 적용한 채팅·UI 상태, 잘못된 패치의 거부 |
| `src/lib/colors.test.ts` | heatmap 색 척도(양 끝, 보간, 범위 밖 값, 결측값), 카테고리 색 |
| `src/App.smoke.test.tsx` | jsdom에서 모의 모드 화면 렌더링과 빠른 질문 4개의 동작 |
| 그 밖의 파일 | 도면 좌표 계산, 렌더링 준비, 관계 그래프 배치, HTTP 클라이언트, 모의 데이터와 시나리오 스트림 |

순수 로직 테스트는 Node 환경에서 실행한다. 화면 테스트만 파일 첫 줄의 `// @vitest-environment jsdom`으로 jsdom을 사용한다. jsdom에는 WebGL이 없으므로 화면 테스트는 deck.gl을 가벼운 대체 모듈로 바꿔 레이어 구성만 확인한다.

## 3. 화면 구성

| 영역 | 기능 |
|---|---|
| 상단 바 | 제목, 개정 선택(`/api/meta`), 개정 비교 켜기와 비교 기준 개정 선택, 층 탭(`/api/meta`의 층 목록) |
| 도구 막대 | 집기·통로·라벨 레이어, 확대·축소·전체 보기, heatmap 끄기, 강조·연결 지우기 |
| 도면 캔버스 | deck.gl `OrthographicView`로 그린다. 매장은 카테고리 색 또는 heatmap 색으로 칠한다. 통로, 수직동선(에스컬레이터·엘리베이터·계단)과 라벨, 편의시설, 출입구, 집기, 매장 라벨, 강조 외곽선, 연결선을 표시한다. |
| 범례 | 카테고리, heatmap 척도, 개정 비교 색, 강조 사유, 연결선 설명을 글자로 표시한다. |
| 매장 상세 카드 | 매장을 누르면 브랜드, 카테고리, 면적, KPI(`/api/analytics/productivity`)를 표시한다. |
| 에이전트 탭 | 메시지 목록, 스트리밍 응답, 도구 호출 칩(인자 펼침), 화면 갱신 기록, 빠른 질문, 입력창 |
| 개정 검토 탭 | 유형별 변경 목록, 영향 분석(생성기, 위험도, 승인 방식), 산출물 목록, 승인·반려 |
| 레이아웃 대안 탭 | 대안 생성 요청, 이동 목록, 목적함수, 매출 영향 범위, 제약 충족 여부, 미리보기 |
| 관계 그래프 탭 | `state.edges`로 만든 force 그래프. 선 굵기는 weight다. 노드를 누르면 도면에서 해당 매장을 선택한다. |

개정 비교를 켜면 비교 기준 개정의 매장 경계를 보라색 선으로 겹쳐 그린다. 바뀐 매장은 `GET /api/changes/{compareRev}/{rev}`의 변경 유형에 따라 칠한다.

| 변경 유형 | 색 |
|---|---|
| 추가(`add`) | 초록 |
| 철거(`remove`) | 빨강. 현재 개정에 없는 매장은 비교 기준 개정의 도형으로 그린다. |
| 이전(`move`), 브랜드 교체(`reassign`) | 주황 |
| 면적 변경(`resize`) | 파랑 |
| 분할(`split`), 병합(`merge`) | 보라 |
| 집기 변경(`fixture_change`) | 청록 |

## 4. 상태 패치와 렌더링

채팅 요청은 `POST /api/chat`으로 보낸다. 응답은 SSE(Server-Sent Events) 스트림이다. `EventSource`는 POST를 지원하지 않으므로 `fetch`와 `ReadableStream`으로 읽는다.

| 이벤트 | 웹앱의 처리 |
|---|---|
| `RUN_STARTED` | `thread_id`를 저장한다. 다음 질문에 같은 `thread_id`를 보낸다. |
| `TEXT_MESSAGE_START/CONTENT/END` | 어시스턴트 말풍선을 만들고 `delta`를 이어 붙인다. |
| `TOOL_CALL_START/END` | 도구 이름과 요약을 칩으로 표시한다. 칩을 펼치면 인자를 볼 수 있다. |
| `STATE_DELTA` | UI 상태에 패치를 적용하고 “화면 갱신” 기록을 남긴다. |
| `RUN_FINISHED`, `RUN_ERROR` | 실행을 끝낸다. 오류는 대화에 표시한다. |

`STATE_DELTA`는 원자적으로 적용한다. 패치의 연산 하나라도 실패하면 이벤트 전체를 버리고 이전 상태를 유지한다. 실패하면 대화에 “화면 갱신 실패”와 원인을 표시한다.

사용자의 화면 조작(층 탭, 개정 선택, 레이어 켜기·끄기, 변경 항목 보기, 대안 미리보기)도 같은 JSON Patch 경로로 상태를 바꾼다.

| 상태 경로 | 렌더링 |
|---|---|
| `/rev` | 도면, KPI, 산출물을 조회할 개정을 바꾼다. |
| `/compareRev` | 값이 있으면 개정 비교 모드로 그린다. `null`이면 비교를 끈다. |
| `/floor` | 층 탭과 도면을 바꾼다. 층이 바뀌면 선택을 해제하고 도면 전체가 보이도록 맞춘다. |
| `/layers/fixtures`, `/layers/walkways`, `/layers/labels` | 집기, 통로, 라벨 레이어를 켜거나 끈다. 집기를 켜면 `?include=fixtures`로 도면을 다시 요청한다. |
| `/heatmap` | `values`가 있으면 매장을 순차 색 척도(0=밝은 노랑, 1=남색)로 칠한다. 값이 없는 매장은 회색이다. `heatmap` 또는 `values`가 `null`이면 카테고리 색으로 칠한다. |
| `/highlights` | 해당 매장에 색 외곽선을 그린다. 사유는 범례, 툴팁, 상세 카드에 표시한다. |
| `/edges` | 매장 중심을 곡선으로 잇는다. 선 굵기는 weight, 글자는 label이다. 한쪽 끝이 다른 층 매장이면 캔버스 오른쪽 가장자리의 층 배지로 잇는다. 배지를 누르면 그 층으로 이동한다. 같은 데이터로 관계 그래프를 그린다. |
| `/focus` | 현재 층에 있는 대상 매장의 경계 상자로 확대·이동한다. 대상 매장이 다른 층에 있으면 그 층을 표시할 때 적용한다. |
| `/panel` | `changes`·`artifact`는 개정 검토 탭, `layout`은 레이아웃 대안 탭을 연다. `table`은 도면 캔버스 아래쪽에 표 패널을 연다. `none`은 탭을 바꾸지 않는다. |

매장의 층은 매장 코드의 앞부분(`{floor}-{band}{nn}`, [CAD 규약](../../docs/specs/cad-conventions.md))으로 판단한다. 예를 들어 `2F-C03`의 층은 `2F`다.

## 5. 모의 데이터

`src/mock/`은 백엔드 없이 시연할 수 있는 합성 데이터와 시나리오를 제공한다.

| 항목 | 내용 |
|---|---|
| 메타 | 가상 플래그십점, 층 2개(1F, 3F), 카테고리 23개 |
| 개정 | R0(2024-10-01, 기준 배치), R1(2025-03-01, S/S MD 개편), R2(2025-09-01, F/W MD 개편), H1(가상 what-if, R2 기반). 시작 개정은 R2다. |
| 도면 | 120 m × 80 m 건물, 링 통로와 아트리움, 층마다 매장 16~19개, 에스컬레이터 2기, 엘리베이터 2기, 계단 2개, 화장실·안내데스크·ATM·고객라운지, 출입구, 집기 |
| 변경 | R0→R1 9건(이전, 면적 변경, 추가, 통로 변경, 브랜드 교체, 병합, 철거, 집기 변경), R1→R2 4건, R2→H1 2건. 다른 개정 쌍은 개정 계보로 합성하거나 역방향으로 변환한다. |
| 영향 분석 | 생성기 스펙 4개(면적표, 매대·집기 목록, 층별 안내도, POS↔공간 KPI)의 트리거로 계산한다. |
| 산출물 | 승인 대기, 게시됨, QA 실패 상태를 포함한다. CSV와 SVG는 내려받을 수 있다. xlsx는 모의 모드에서 제공하지 않는다. |
| 생산성 | 에스컬레이터 거리와 브랜드 집객력으로 계산한 합성 평효율, 백분위, 저효율 여부, 요인 |
| 레이아웃 대안 | 1F와 3F에 대안 3개씩 |

빠른 질문은 아래 시나리오를 실행한다. 시나리오는 도구 호출 칩과 `STATE_DELTA`를 백엔드와 같은 형식으로 보낸다.

| 질문 | 화면 변화 |
|---|---|
| S/S 개편 도면 변경점과 재생성 대상 알려줘 | R1과 R0를 비교하고, 변경 매장을 강조하고, 개정 검토 탭을 연다. |
| 3층 저효율 zone과 원인 | 평효율 heatmap, 저효율 매장 2개 강조, 연결선 3개(1F 연결 포함), 확대 이동 |
| 40대 VIP 교차구매 보여줘 | 교차구매 연결선 5개(층간 연결 포함)와 중심 브랜드 강조. 관계 그래프 탭에 같은 관계를 표시한다. |
| 3층 재배치안 3개 비교 | 레이아웃 대안 탭에 대안 3개를 열고, 추천안의 이동 경로를 도면에 표시한다. |

이 밖에 “3층 평효율 표로 보여줘”는 표 패널(`panel.type = table`)을 연다. 매장 코드(예: `3F-C02`)를 넣은 질문은 해당 매장의 KPI를 요약한다.

## 6. 폴더 구조

```text
apps/atlas-web/
  index.html, vite.config.ts, tsconfig*.json, package.json
  public/            파비콘
  src/
    App.tsx          데이터 소스 결정(실제 백엔드 또는 모의 데이터)과 화면 골격
    api/             REST·SSE 클라이언트, 모의 모드 결정, 캐시 키
    state/           UI 상태 모델, 정규화, 리듀서, React 컨텍스트
    lib/             JSON Patch, SSE 파서, 색, 좌표 계산, 그래프 배치, 표시 형식
    map/             deck.gl 캔버스, 레이어 구성, 렌더링 준비(순수 함수)
    components/      상단 바, 도구 막대, 범례, 상세 카드, 채팅, 개정 검토, 레이아웃 대안, 관계 그래프, 표
    mock/            합성 메타·도면·변경·산출물·생산성·대안과 채팅 시나리오
    types/api.ts     API 계약의 TypeScript 타입
```

## 7. 계약 해석

계약에 명시되지 않은 부분은 아래와 같이 처리한다. 계약 문서를 바꿀 때 함께 검토한다.

| 항목 | 웹앱의 처리 |
|---|---|
| heatmap 끄기 | `heatmap: null`과 `heatmap.values: null`을 모두 “끔”으로 처리한다. |
| SSE 이벤트 구분 | 표준 형식(`data: {...}` 뒤 빈 줄)을 처리한다. 빈 줄 없이 한 줄에 JSON 하나를 보내도 처리한다. |
| 필드 이름 | 계약의 snake_case(`thread_id`, `message_id`, `tool_call_id`, `tool_name`)를 기준으로 한다. AG-UI SDK의 camelCase(`threadId`, `toolCallName` 등)도 받는다. |
| 알 수 없는 이벤트 | 계약에 없는 이벤트(예: `TOOL_CALL_ARGS`, `STATE_SNAPSHOT`)는 무시한다. |
| `facility` 형식 | 문자열 또는 `{code, name}` 객체를 받는다. |
| 카테고리 색 | `color`가 없으면 백엔드 층별 안내도 생성기와 같은 방식으로 색을 만든다. |
| `panel.payload` | `changes`: `{rev_from, rev_to}`, `artifact`: `{generator}`, `layout`: `{rev, floors, alternatives}`, `table`: `{title, columns, rows}`(행은 객체 또는 배열)로 해석한다. 키가 없으면 현재 상태로 대신한다. |
| 다른 층의 `focus` | 현재 층에 대상 매장이 없으면 그 층을 표시할 때 적용한다. |
| 양 끝이 모두 다른 층인 `edges` | 도면에는 그리지 않고 관계 그래프에만 표시한다. |
| `percentile` | 0~1과 0~100을 모두 받아 0~100으로 표시한다. |
| 산출물 파일 `url` | 없으면 `/api/artifacts/{rev}/{generator}/{path}`로 만든다. |
| 검토 오류 | 오류 응답의 `detail`을 산출물 카드에 표시한다. |
| 레이아웃 `constraints` | `{max_moves, keep_anchors}`를 보낸다. 작업 중인 백엔드 구현이 제약을 키워드 인자로 받으므로 키 이름을 그 구현에 맞췄다. |
| 레이아웃 대안 형식 | 계약 형식(`id`, `kpi_delta`, `constraints`)과 레이아웃 엔진 형식(`alt_id`, `delta_krw`, `objective_low/high`, `constraint_checks`)을 모두 받아 같은 화면으로 표시한다. |
| 채팅 `ui_state` | 계약대로 `rev`, `floor`, `selection`만 보낸다. |

## 8. 접근성

- 모든 버튼에는 글자 또는 `aria-label`이 있다.
- 키보드 포커스는 파란 외곽선으로 표시한다.
- 층 탭과 패널 탭은 화살표 키, Home, End로 이동한다.
- 범례와 제약 조건은 색과 함께 글자로 의미를 표시한다.
- 관계 그래프의 노드는 Tab과 Enter로 조작한다. 같은 관계를 표(연결 목록)로도 제공한다.
- 한글 입력 중 조합을 확정하는 Enter는 메시지를 보내지 않는다.

## 9. 제한 사항

- 모의 데이터는 1F와 3F만 제공한다. 다른 층의 매장은 층 배지와 관계 그래프에서 매장 코드로 표시한다.
- 화면 테스트는 WebGL 렌더링 결과를 검사하지 않는다. 도면 렌더링은 브라우저에서 확인한다.
- 빌드의 기본 JavaScript 파일은 약 1 MB(gzip 약 300 KB)다. 대부분 deck.gl이다.
