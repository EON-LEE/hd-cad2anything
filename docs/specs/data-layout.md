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

## 4. 금지 사항

- 실제 개인정보
- 실제 브랜드 매출
- 라이선스가 제한된 원본(dunnhumby, H&M, AI Hub, OSM 좌표)
- 실제 브랜드 매핑. 실제 브랜드 매핑은 `data/local/`(Git 제외)에만 둔다.
