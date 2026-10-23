# 데이터 배치 규약

## 1. 생성 데이터 위치

생성 데이터는 `data/generated/`에 둔다. 이 폴더는 Git에서 제외되며, 스크립트로 다시 만든다.

```text
data/generated/
  drawings/{rev}/{variant}/manifest.json, flagship_{rev}_{floor}.dxf
  truth/layout_{rev}.json                    # SpatialModel.to_dict()
  truth/changes_{from}_{to}.json             # ChangeSet.to_dict()
  business/categories.csv
  business/brands.csv                        # 가상 브랜드
  business/tenancies.csv                     # brand_id, unit_code, rev_id, valid_from, valid_to, commission_rate
  business/pos_terminals.csv                 # pos_id, brand_id, unit_code, valid_from, valid_to
  business/customers.csv                     # pseudo_id, age_band, gender, tier, join_date, home_area
  business/calendar.csv
  business/transactions.csv.gz               # txn_id, ts, pos_id, customer_id(nullable), amount_krw, items, payment, is_return, ref_txn_id
  business/footfall_zone_hourly.csv.gz       # 선택
  business/planted_effects.json              # 심은 패턴과 기대 신호
  business/data_card.md
```

### 1.1 작업공간

`bootstrap`이 생성 데이터로 작업공간(`data/workspace/`, Git 제외)을 만든다. Atlas는 이 작업공간을 PostgreSQL에 미러하고, 업로드한 파일과 처리 결과도 같은 구조로 저장한다.

```text
data/workspace/
  drawings/{rev}/{clean|messy|uploaded}/      # 도면. 업로드한 도면은 uploaded
  models/{rev}.json                          # 공간 모델(실제·가상 개정)
  changes/{from}__{to}.json                  # 변경 집합
  reports/{rev}.json                         # 파싱 보고서
  business/*.csv[.gz]                        # 업무 표(브랜드, 테넌시, POS, 고객, 거래)
  business/leases.csv                        # 현재 계약: lease_id, brand_id, unit_code, lease_start, lease_end, contract_area_m2, commission_rate
  business/stores.csv                        # 비교 점포 목록: store_id, store_name, region, format, gross_area_m2, opened
  business/store_kpis.csv                    # 비교 점포 월별 실적: store_id, period(YYYY-MM), floor, category, area_m2, sales_krw
  business/relayout_briefs/{brief_id}.yaml   # 시즌 개편 브리프(사람 입력)
  business/drawing_profiles/{id}.yaml        # 도면 규약 프로필(사람 입력)
  business/_backup/                          # 메타데이터를 교체하기 전 파일
  artifacts/{rev}/{generator}/               # 산출물
  projects/relayout/{brief_id}.json|.html    # 개편 프로젝트(진단, 대안, 결정, 공사 순서, 도면 대조)와 제안서
  uploads/jobs/{job_id}.json                 # 도면 업로드 작업 상태와 결과
```

`leases.csv`와 `FW2026` 브리프는 합성 데이터다. `bootstrap`이 없을 때 만들고(`cad2anything.mockgen.relayout`), 계약 면적·브랜드 불일치 4건을 일부러 심는다(6F-C14, 2F-N06, 7F-N05 면적, 3F-C13 브랜드).

`stores.csv`와 `store_kpis.csv`는 가상 점포 6곳의 합성 실적이다. `bootstrap`이 둘 다 없을 때 대상 점포의 실제 거래 규모를 기준으로 만든다(`cad2anything.mockgen.stores`). 사람이 올린 표는 덮어쓰지 않는다.

## 2. 개정과 기간

| 개정 | 종류 | 적용일 | 설명 |
|---|---|---|---|
| `R0` | actual | 2024-10-01 | 기준 배치 |
| `R1` | actual | 2025-03-01 | S/S MD 개편 |
| `R2` | actual | 2025-09-01 | F/W MD 개편 |
| `H1` | hypothetical | — | `R2` 기반 what-if 예시 |

거래 데이터 기간은 2024-10-01부터 2026-09-30까지 24개월이다.

## 3. 저장소에 커밋하는 샘플

`data/samples/`에는 소형 샘플만 커밋한다.

- 1개 층의 clean·messy DXF
- 매니페스트
- 브랜드와 테넌시
- 거래 수천 건
- `metadata/`: 업로드 시연용 계약 정보(`leases.csv`), 개편 브리프(`FW2026.yaml`), 비교 점포 목록(`stores.csv`)과 점포 실적(`store_kpis.csv`)
- `uploads/`: 기본 규약과 다른 "고객사 형식" 합성 도면(ZIP)과 그 도면 규약 프로필

## 4. 금지 사항

- 실제 개인정보
- 실제 브랜드 매출
- 라이선스가 제한된 원본(dunnhumby, H&M, AI Hub, OSM 좌표)
- 실제 브랜드 매핑. 실제 브랜드 매핑은 `data/local/`(Git 제외)에만 둔다.
