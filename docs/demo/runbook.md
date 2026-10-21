# 데모 런북

## 1. 목적과 범위

이 문서는 CAD2Anything PoC 데모를 준비하고, 진행하고, 정리하는 절차다. 독자는 데모 진행자다.

- 모든 도면과 거래 데이터는 합성 데이터다. 브랜드 이름은 가상이다.
- 명령은 저장소 루트에서 PowerShell로 실행한다.
- 리소스 이름은 `infra/README.md`의 배포 출력값을 쓴다. 이 문서에는 실제 주소와 구독 정보를 적지 않는다.

## 2. 구성 요약

| 구성 요소 | 역할 |
|---|---|
| Atlas 서비스(Container App 1개) | 웹 화면, REST·채팅 API, MCP 엔드포인트(`/mcp`) |
| Foundry 에이전트 7개 | Orchestrator와 전문 에이전트 6개. 구성은 `agents/agents.yaml`에 있다. |
| PostgreSQL + PostGIS | 작업공간 미러(`workspace_file` 테이블)와 공간 질의 |
| 모델 배포 | `gpt-6.1-sol`(250K TPM), `gpt-5.4-mini`(150K TPM) |

## 3. 사전 준비

**전제조건**

- `az login`으로 데모 구독에 로그인되어 있다.
- 개발자 계정에 `infra/README.md`의 개발자 역할이 할당되어 있다.
- `uv`, Node.js, Azure CLI가 설치되어 있다.
- 개발 PC의 공인 IP가 PostgreSQL 방화벽 규칙에 등록되어 있다.

**절차**

1. 합성 도면을 생성한다.

   ```powershell
   uv run --no-sync python -m cad2anything.mockgen.drawings --out data/generated --seed 42
   ```

2. 합성 업무 데이터를 생성한다. 기본 표본 비율은 0.02다.

   ```powershell
   uv run --no-sync python -m cad2anything.mockgen.business --out data/generated --seed 42
   ```

3. 미러 설정을 지정한다.

   ```powershell
   $env:WORKSPACE_MIRROR = 'postgres'
   $env:POSTGRES_HOST = '<PostgreSQL 서버 FQDN>'
   $env:POSTGRES_USER = (az account show --query user.name -o tsv)
   ```

4. 도면을 파싱해 작업공간을 만들고 미러에 올린다.

   ```powershell
   uv run --no-sync python -m cad2anything.bootstrap --mode parse --push
   ```

5. 공간 질의용 PostGIS 테이블을 동기화한다.

   ```powershell
   uv run --no-sync python -m cad2anything.store.postgis sync --root data/workspace
   ```

6. 점검표를 실행한다. 모든 항목이 `통과`인지 확인한다. 하나라도 `실패`이면 데모를 진행하지 않고 원인을 확인한다.

   ```powershell
   uv run --no-sync python -m cad2anything.evaluation.scorecard --root data/workspace --truth data/generated/truth
   ```

7. 이미지를 빌드하고 서비스와 에이전트를 배포한다. 절차는 `infra/README.md`의 "서비스 배포"를 따른다.
8. 서비스가 작업공간을 읽었는지 확인한다. `GET <atlas-url>/healthz`의 `revisions`가 `["R0","R1","R2","H1"]`이어야 한다.
9. 데모 전에 4절의 질문 하나를 실행한다. 첫 분석 요청은 거래 데이터를 메모리에 올리므로 다른 요청보다 느리다.

**완료 기준:** 6단계의 점검표가 모두 통과하고, 8단계의 `revisions`가 네 개이고, 9단계의 질문이 `RUN_FINISHED`로 끝난다.

## 4. 데모 시나리오

질문은 Atlas 채팅 창에 입력한다. 응답 시간은 도구 호출 수에 따라 달라진다. 2026-10-21 평가에서는 대부분의 질문이 60초 안에 끝났고, 레이아웃 대안과 업계 조사처럼 전문 에이전트나 웹 검색을 거치는 질문은 100~140초가 걸렸다. 진행자는 응답을 기다리는 동안 채팅에 표시되는 도구 호출을 설명한다.

| 순서 | 시나리오 | 질문 예시 | 화면에서 보여줄 것 | 근거 점검 |
|---|---|---|---|---|
| 1 | 도면 개정 반영 | S/S 개편 도면(R1)이 올라왔어. R0와 비교해서 뭐가 바뀌었고 무엇을 다시 만들어야 해? | 변경 목록, 재생성 대상 산출물, 개정 검토 탭의 승인 대기 산출물 | R0→R1 diff 정확도 |
| 2 | 공간 생산성 진단 | R2 기준 4층에서 공간 효율이 낮은 매장을 찾아 원인을 설명하고 도면에 표시해줘. | 저효율 히트맵, 강조된 4층 매장, 요인별 영향 | 저효율 존 회수 |
| 3 | 고객 교차구매 | 40대 VIP·VVIP 여성 고객이 화장품과 함께 많이 사는 카테고리는 뭐야? 관계를 화면에 그려줘. | 카테고리 관계선, 결과 표 패널 | 세그먼트 교차구매 회수 |
| 4 | 상관과 인과 구분 | R2에서 아동 매장 근처 매장들 매출이 높아 보이는데, 아동 매장을 늘리면 주변 매출도 오를까? | 노출을 통제하기 전후의 효과 비교 | 노출 함정 판정 |
| 5 | 레이아웃 대안 | R2 기준 4층 저효율 구역을 개선하는 재배치 대안 3개를 만들어 비교해줘. 앵커는 유지하고 면적 허용 오차는 ±10%로 해줘. | 레이아웃 탭의 대안 비교, 가상 개정 도면 | 레이아웃 대안 제약 충족 |
| 보조 | 즉석 산출물 | R2 기준 층별 청소 구역표를 만들어줘. 층, 구역, 면적 합계가 있으면 돼. | 초안 표와 레지스트리 등록안 | — |
| 보조 | 업계 조사 | 최근 국내 백화점의 층별 MD 개편 트렌드를 공개 자료로 요약하고 출처를 붙여줘. | 출처가 붙은 요약 | — |

**설명할 때 지킬 것**

- 매출과 ㎡당 매출은 합성 표본 거래를 전체 규모로 환산하고 연 단위로 환산한 값이라고 밝힌다.
- 요인 분해, 인접 효과, 레이아웃 대안은 추정치다. 인과 효과로 설명하지 않는다.
- 산출물 승인은 에이전트가 하지 않는다. 사람이 개정 검토 탭에서 승인하거나 반려한다.

## 5. 문제 대응

| 증상 | 확인할 것 | 조치 |
|---|---|---|
| 채팅이 `RateLimitError`로 끝난다. | 모델 배포의 분당 토큰 한도 | 1분 뒤 같은 질문을 다시 보낸다. 반복되면 모델 배포 용량을 확인한다. |
| `/healthz`의 `revisions`가 빈 목록이다. | 미러 적재 여부 | 3절 4단계를 다시 실행한 뒤 Container App 리비전을 다시 시작한다. |
| 생산성 결과가 "가상 개정에는 거래 실적이 없습니다"로 끝난다. | 선택한 개정의 종류 | R0, R1, R2 중 하나를 선택한다. 가상 개정은 레이아웃 도구로 추정한다. |
| 앱 로그에 `/mcp` 401 응답이 있다. | 앱 비밀과 Foundry 프로젝트 연결의 MCP 키 | `infra/services.bicep`을 다시 배포한다. 이 배포는 두 값을 함께 바꾼다. |
| 히트맵이 비어 있다. | 개정과 층, 지표 | 가상 개정에서는 `sales_per_m2`와 `sales_krw`만 기대 매출 추정치로 표시된다. |

## 6. 데모 초기화

데모 중 만든 가상 개정(H2 이후)과 산출물 승인 상태는 미러에 저장되어 앱을 다시 시작해도 남는다. 다음 데모를 처음 상태에서 시작하려면 아래 절차를 실행한다.

> **주의:** 이 절차는 미러에서 기준 데이터에 없는 파일을 모두 삭제한다. 보존할 가상 개정이나 산출물이 있으면 먼저 내려받는다.

1. 3절 3단계의 미러 설정을 지정한다.
2. 작업공간을 처음부터 다시 만들고, 미러에 올리고, 기준 데이터에 없는 미러 파일을 삭제한다.

   ```powershell
   uv run --no-sync python -m cad2anything.bootstrap --mode parse --clean --push --prune
   ```

3. Container App 리비전을 다시 시작한다. 새 컨테이너가 미러에서 작업공간을 내려받는다.

   ```powershell
   az containerapp revision restart --resource-group <리소스 그룹> --name <Container App 이름> --revision <활성 리비전 이름>
   ```

4. `GET <atlas-url>/healthz`의 `revisions`가 `["R0","R1","R2","H1"]`인지 확인한다.

## 7. 비용 통제와 정리

**상시 비용이 드는 리소스:** Container App(복제본 1개 상시 실행), PostgreSQL(B1ms), Container Registry(Basic), Log Analytics, Storage, Key Vault. 모델 배포(GlobalStandard)는 사용한 토큰만큼 과금된다.

**데모가 없는 기간**

1. PostgreSQL 서버를 중지한다. 중지한 서버는 최대 7일 뒤 자동으로 다시 시작된다.

   ```powershell
   az postgres flexible-server stop --resource-group <리소스 그룹> --name <PostgreSQL 서버 이름>
   ```

2. 데모를 다시 시작할 때 PostgreSQL 서버를 시작한 뒤 Container App 리비전을 다시 시작한다. 앱은 시작할 때 미러에서 작업공간을 내려받는다.

**PoC 종료**

> **경고:** 다음 명령은 리소스 그룹의 모든 리소스와 데이터를 삭제한다. 되돌릴 수 없다. 실행 전에 필요한 결과물을 저장한다.

```powershell
az group delete --name <리소스 그룹>
```

## 8. 알려진 한계

- 실제 도면과 거래 데이터로 검증하지 않았다. 고객 도면으로 파싱 규칙을 확인해야 한다.
- DXF만 지원한다. DWG 변환 경로와 외부 DWG 테스트 도면은 라이선스를 확인한 뒤 결정한다.
- Atlas 웹과 API에는 사용자 인증이 아직 없다. MCP 엔드포인트만 키로 보호한다. 데모 주소를 공유하기 전에 인증 적용 여부를 결정해야 한다.
- 도면 업로드 이벤트를 자동으로 처리하지 않는다. 개정 처리는 API(`POST /api/revisions/{rev}/process`)로 실행한다.
- 작업공간을 메모리에 두므로 복제본을 1개로 고정한다.
