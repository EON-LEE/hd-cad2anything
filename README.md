# hd-cad2anything

이 저장소는 백화점 CAD 도면을 버전 관리되는 **공간관계 모델**로 이해하는 PoC다. Microsoft Foundry 에이전트가 이 모델을 기반으로 아래 작업을 수행한다.

- **도면 변경 재생성:** 개정을 해석하고 영향을 분석한 뒤, 매대·면적표, 안내도, KPI를 재생성한다.
- **공간 생산성 진단:** 층, zone, 매장 단위로 공간 생산성을 진단한다.
- **레이아웃 대안:** 레이아웃 대안을 생성하고 비교한다(what-if).

모든 데이터는 합성 데이터이고, 브랜드명은 모두 가상이다.

## 문서

| 문서 | 내용 |
|---|---|
| [시장조사](docs/research/market-research.md) | 경쟁 지형, 기술, 차별화 근거 |
| [아키텍처](docs/architecture.md) | 계층, 에이전트, 개정 루프, Azure 구성 |
| [요구사항 탐색](docs/requirements/discovery.md) | 부서별 가설, 우선순위, 백로그 |
| [CAD 규약](docs/specs/cad-conventions.md) | 합성 도면과 파싱의 계약 |
| [데이터 배치](docs/specs/data-layout.md) | 생성 데이터의 경로와 형식 |

## 구조

```text
src/cad2anything/  Python 패키지: 모델 계약, 목데이터, CAD 이해, 분석, 레이아웃, 도구
tests/             pytest
docs/              조사·설계·규약 문서
infra/             Azure IaC(Bicep, azd)
agents/            Foundry 에이전트 정의
services/          MCP 서버, UI 브리지, 개정 워커
apps/              Atlas 웹앱
eval/              평가 세트
data/              보정 출처, 소형 샘플. 생성 데이터와 로컬 매핑은 Git에서 제외한다.
```

## 개발 환경

```powershell
uv sync
uv run pytest
uv run ruff check src tests
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