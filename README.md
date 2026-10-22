# hd-cad2anything

이 저장소는 백화점 CAD 도면을 버전 관리되는 **공간관계 모델**로 이해하는 PoC다. Microsoft Foundry 에이전트가 이 모델을 기반으로 아래 작업을 수행한다.

- **도면 변경 재생성:** 개정을 해석하고 영향을 분석한 뒤, 매대·면적표, 안내도, KPI를 재생성한다.
- **공간 생산성 진단:** 층, zone, 매장 단위로 공간 생산성을 진단한다.
- **레이아웃 대안:** 레이아웃 대안을 생성하고 비교한다(what-if).

모든 데이터는 합성 데이터이고, 브랜드명은 모두 가상이다.

## 현재 구성

- **목데이터:** 9개 층, 약 410개 매장의 합성 DXF 도면(R0·R1·R2 실제 개정, H1 가상 개정)과 24개월 합성 거래 데이터. 거래 데이터에는 분석으로 회수할 수 있는 효과 8종을 심었다.
- **CAD 이해와 개정 루프:** DXF 파싱, 공간관계 모델, 의미 단위 diff, 생성기 레지스트리, 사람 승인.
- **분석과 레이아웃:** 공간 생산성, 요인 분해, 교차구매, 층간 이동, 고객 한 명의 하루 동선(결제 순서 기반 추정), 인접 효과, 개편 전후 비교, 레이아웃 대안, what-if.
- **공간 질의:** 방향, 에스컬레이터 정면, 코너, 메인 동선 같은 공간 어휘를 매장 속성으로 계산한다. 에이전트는 조건·관계·집계 질의, 길 찾기, 층 설명 도구를 조합해 자유 질문에 답한다.
- **에이전트:** Foundry prompt agent 7개. 도메인 도구 26개를 MCP로 쓰고, 화면 제어와 전문 에이전트 상담은 함수 도구로 쓴다.
- **Atlas:** 웹 화면, REST·채팅 API, MCP 엔드포인트를 Container App 하나로 제공한다. 채팅은 답변을 생성되는 대로 보여 주고, 진행 단계는 한국어 한 줄로 요약한다.

## 문서

| 문서 | 내용 |
|---|---|
| [시장조사](docs/research/market-research.md) | 경쟁 지형, 기술, 차별화 근거 |
| [아키텍처](docs/architecture.md) | 계층, 에이전트, 개정 루프, Azure 구성 |
| [요구사항 탐색](docs/requirements/discovery.md) | 부서별 가설, 우선순위, 백로그 |
| [CAD 규약](docs/specs/cad-conventions.md) | 합성 도면과 파싱의 계약 |
| [CAD 파이프라인](docs/specs/cad-pipeline.md) | 파싱 단계, 레이어 분류, 검토 대기열 |
| [데이터 배치](docs/specs/data-layout.md) | 생성 데이터의 경로와 형식 |
| [합성 업무 데이터](docs/data/mock-business-data.md) | 생성기 설계, 보정 근거, 심은 효과 |
| [MCP 도구](docs/specs/mcp-tools.md) | 도메인 도구 계약과 에이전트별 허용 도구 |
| [Atlas API](docs/specs/atlas-api.md) | REST·채팅 API와 UI 상태 계약 |
| [인프라](infra/README.md) | Azure 리소스, 권한, 배포 절차 |
| [평가](eval/README.md) | 점검표와 에이전트 골든 질문 평가 |
| [데모 런북](docs/demo/runbook.md) | 데모 준비, 시나리오, 문제 대응, 비용 통제 |

## 구조

```text
src/cad2anything/  Python 패키지: 모델 계약, 목데이터, CAD 이해, 분석, 레이아웃, 도구, 서비스, 평가
tests/             pytest
docs/              조사·설계·규약·데모 문서
infra/             Azure IaC(Bicep, azd)
agents/            Foundry 에이전트 정의와 UI 함수 도구 정의
services/atlas/    Atlas 컨테이너 이미지(Dockerfile)
apps/atlas-web/    Atlas 웹앱(React, deck.gl)
eval/              골든 질문 세트와 평가 안내
data/              보정 출처, 소형 샘플. 생성 데이터와 로컬 매핑은 Git에서 제외한다.
```

## 개발 환경

```powershell
uv sync --all-extras
uv run pytest
uv run ruff check src tests
```

로컬에서 Atlas를 실행하려면 작업공간을 만든 뒤 서버를 시작한다. 웹앱 개발 서버(`apps/atlas-web`)는 `/api`를 이 서버로 전달한다.

```powershell
uv run python -m cad2anything.mockgen.drawings --out data/generated --seed 42
uv run python -m cad2anything.mockgen.business --out data/generated --seed 42
uv run python -m cad2anything.bootstrap --mode parse
uv run uvicorn cad2anything.services.app:create_app --factory --port 8000
```

PyPI 직접 접근이 막힌 환경에서는 해당 명령에만 조직 미러를 지정한다(`UV_DEFAULT_INDEX`). 이때 생성되는 `uv.lock`은 미러 주소를 기록하므로 커밋하지 않는다.

## 데이터 정책

- 실제 고객 데이터, 실제 브랜드 매출, 개인정보를 커밋하지 않는다.
- 실제 브랜드 매핑은 `data/local/`에만 둔다. 이 폴더는 Git에서 제외된다.
- 공개 통계는 집계 분포 보정에만 쓰고 출처를 표시한다.

## GitHub authentication

Keep `eonlee_MSDEMO` as the default GitHub CLI account. For `EON-LEE/*`
repositories, use the existing EON-LEE Git credentials for fetch and push; do
not add `~/.gitconfig-eonlee-auth` again with `git -c include.path=...`. For
GitHub CLI write operations, scope the EON-LEE token to the individual command
by obtaining it with `gh auth token --user EON-LEE`, and remove `GH_TOKEN`
immediately afterward.