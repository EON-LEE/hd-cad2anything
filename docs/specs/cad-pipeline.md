# CAD 이해 파이프라인과 개정 비교

이 문서는 `cad2anything.cadcore`(도면 → `SpatialModel`)와 `cad2anything.revision`(두 개정 → `ChangeSet`)의 동작을 설명한다. 도면 규약은 [cad-conventions.md](cad-conventions.md)를 따른다. 파이프라인은 그 규약 목록을 알지 못한다고 가정하고, DXF와 매니페스트의 내용만으로 판단한다.

기하, 면적, 매칭, 비교는 결정론적 코드가 계산한다. LLM은 의미가 애매한 레이어의 분류만 돕는다. LLM 사용은 선택 사항이며, 결과에는 신뢰도와 근거가 함께 남는다.

## 1. 처리 단계

```text
manifest.json + 층별 DXF
  → reader    : 단위 환산, 문자 디코딩·복구, 원시 요소 추출
  → geometry  : 레이어별 폴리곤 복원과 QA
  → classify  : 레이어 의미 판정(매니페스트 → 휴리스틱 → LLM → 사람 검토)
  → labels / fixtures : 라벨·집기를 공간에 매칭
  → pipeline  : 엔티티, 브랜드 레지스트리, 관계 → SpatialModel + ParseReport
  → revision.diff : 두 개정의 SpatialModel 비교 → ChangeSet
```

1. **읽기(`reader.py`).** 매니페스트의 `levels`에서 층과 파일을 찾는다. 매니페스트가 없으면 파일명(`..._3F.dxf`)에서 층을 추정한다.
2. **단위 환산.** `$INSUNITS`로 미터 환산 계수를 정한다(4=mm → 0.001, 5=cm → 0.01, 6=m → 1). 값이 없거나 알 수 없으면 매니페스트 `units`, 그다음 도면 범위 순으로 추정하고 `units_inferred` 이슈를 남긴다.
3. **문자 처리.** `\U+xxxx` 이스케이프를 디코딩한다. CP949 바이트가 서유럽 코드페이지로 잘못 해석된 문자열은 CP949로 다시 해석하고 `mojibake_fixed`로 표시한다. 복구하지 못한 문자열은 `mojibake_suspect`(error)로 표시한다.
4. **원시 요소 추출.** 닫힌·열린 LWPOLYLINE/POLYLINE, LINE, TEXT/MTEXT, INSERT를 미터 좌표로 읽는다. MTEXT의 `\P`는 줄바꿈으로 바뀐다. 문자는 정렬 방식과 글자 폭으로 중심점을 추정한다. INSERT는 블록명, 삽입점, 회전, 속성, 블록 외곽(블록 안의 가장 큰 닫힌 폴리라인 또는 bbox)을 가진다.
5. **기하 복원(`geometry.py`).** 레이어마다 폴리곤을 만든다. 세부 기준은 3절에 있다.
6. **레이어 분류(`classify.py`).** 층·레이어마다 내용 특징을 계산하고 의미를 판정한다. 세부 전략은 2절에 있다.
7. **라벨·집기 매칭(`labels.py`, `fixtures.py`).** 문자의 추정 중심점이 들어 있는 폴리곤에 라벨을 배정한다. 포함되지 않으면 허용 거리 안의 가장 가까운 폴리곤에 배정한다.
8. **엔티티·관계 구성(`pipeline.py`).** 엔티티 ID 규약, 브랜드 레지스트리 조회, 관계 계산은 4절에 있다.

## 2. 레이어 분류 전략

레이어 의미는 다음 16개 중 하나다: `exterior`, `wall`, `unit`, `unit_label`, `zone`, `zone_label`, `walkway`, `escalator`, `elevator`, `stair`, `opening`, `amenity`, `amenity_label`, `fixture`, `annotation`, `noise`. 판정 단위는 (층, 레이어)다. 같은 이름의 레이어도 층마다 내용이 다를 수 있기 때문이다.

레이어 이름, 매장 코드 체계, 단위가 고객사마다 다르면 [도면 규약 프로필](drawing-profile.md)로 맞춘다. 프로필의 레이어 매핑은 매니페스트 `layers`에 합쳐지고, 매장·존·수직동선·출입구 코드는 프로필의 패턴으로 읽어 표준 코드로 바꾼다. 사람이 업로드 작업의 검토 큐에서 고친 층별 매핑은 매니페스트 `layers_by_floor`에 저장되고 다른 매핑보다 먼저 쓰인다. 프로필을 지정하지 않으면 기본 프로필(`default`, 이 문서의 합성 도면 규약)을 쓴다.

| 순서 | 단계 | 동작 | 결과 표시 |
|---|---|---|---|
| 1 | 매니페스트 | 매니페스트 `layers`(의미 → 레이어 이름)에 있는 레이어를 그대로 사용한다. | `method=manifest`, 신뢰도 1.0 |
| 2 | 휴리스틱 | 레이어명 키워드와 내용 특징을 결합한다. | `method=heuristic` |
| 3 | LLM(선택) | 신뢰도가 0.7 미만인 레이어만 질의한다. | `method=llm` 또는 `heuristic+llm` |
| 4 | 사람 검토 | 여전히 0.7 미만인 레이어를 검토 큐에 넣는다. | `review_queue`, 이슈 `layer_needs_review` |

### 2.1 휴리스틱

- **레이어명 키워드:** 한글·영문 사전(예: `벽`·`WALL`, `구획`·`TENANT`, `통로`·`CORR`, `계단`·`STAIR`, `치수`·`DIM`)으로 사전 점수 0.35~0.6을 준다. 겹치는 한글 키워드는 긴 키워드를 우선한다(예: `외벽선`은 벽이 아니라 외곽). `TXT`, `NAME`, `번호`, `문자`, 끝 글자 `명` 같은 문자 표시가 매장·존·편의시설 키워드와 함께 있으면 해당 `*_label`로 본다.
- **내용 특징:** 다음 특징으로 의미별 점수를 계산한다.

| 특징 | 판단에 쓰는 예 |
|---|---|
| 닫힌 폴리곤 수와 면적 분포 | 가장 큰 폴리곤이 층의 기준 외곽(그 층에서 가장 크고 다른 요소를 포함하는 폴리곤) 면적의 85% 이상이고 다른 레이어 폴리곤의 80% 이상을 포함하면 `exterior` |
| 폴리곤 안의 매장 코드 문자 수 | 폴리곤마다 코드가 약 1개면 `unit`, 평균 1.3개를 넘으면 여러 매장을 묶은 `zone` |
| 문자 패턴 | 매장 코드 `^(B\d\|\d+F)-[NSEWCP]\d{2}[A-Z]?M?$`, 존 코드 `{층}-Z{n}`, `ESC-`/`EV-`/`ST-`, `ENT-`, `WC`/`INFO`/`ATM`/`NURSING`/`LOUNGE`, 숫자만 있는 치수, 도면 제목 |
| 라벨 없는 폴리곤의 형태 | 합집합이 하나로 이어지고 길쭉하면 `walkway` |
| 선 통계 | 외곽선 위의 선은 `opening`, 다른 레이어의 공간 경계와 겹치는 닫힌 영역은 `wall`, 건물 밖·임의 방향 선은 `noise` |
| INSERT 비율 | 레이어 요소의 60% 이상이 블록 참조면 `fixture` |

- **결합:** 의미마다 이름 점수와 내용 점수를 noisy-OR로 합친다. 최종 신뢰도는 최고 점수에서 가장 강한 경쟁 의미 점수의 0.25배를 뺀 값이다. 근거 문장과 경쟁 의미(`alternatives`)를 함께 기록한다.
- **층 단위 보정:** 한 층에 `exterior`가 여럿이면 가장 큰 레이어만 유지한다. `unit` 레이어가 여럿이면 코드 밀도가 높은 레이어를 `zone`으로 바꾼다.

### 2.2 LLM 보조

`LlmClassifier(client, model)`는 레이어 특징 요약(요소 수, 폴리곤 통계, LINE 루프가 겹치는 다른 레이어 이름, 문자 종류, 40자 이내 샘플 문자 최대 8개, 선 통계, 블록명)과 특징 해설을 보낸다. 휴리스틱 추정값은 보내지 않는다. 응답은 엄격한 JSON 스키마 `{layer, semantic, confidence, evidence}`를 따라야 한다.

LLM 의견은 다음 규칙으로 반영한다(`classify.merge_second_opinion`).

| 경우 | 처리 | 검토 큐 |
|---|---|---|
| LLM 판정이 휴리스틱 판정과 같다 | 두 신뢰도를 noisy-OR로 합친다. `method=heuristic+llm` | 합친 신뢰도가 0.7 이상이면 빠진다 |
| 판정이 다르고 휴리스틱 신뢰도가 0.4 미만이다 | LLM 신뢰도가 더 높으면 LLM 판정을 채택한다. `method=llm` | LLM 신뢰도가 0.7 이상이면 빠진다 |
| 그 밖의 불일치(휴리스틱 신뢰도 0.4 이상 등) | 휴리스틱 판정과 신뢰도를 유지한다. LLM 판정은 경쟁 의미와 근거에 기록한다. | 남는다(사람이 결정) |
| 호출 실패, 스키마 위반 | 의견 없음으로 처리하고 오류를 보고서 `stats.llm.errors`에 남긴다. | 남는다 |

LLM 호출은 CLI의 `--llm` 또는 `parse_revision(..., classifier=...)`를 지정할 때만 일어난다. 실제 고객 도면에 사용하기 전에 샘플 문자 전송이 데이터 정책에 맞는지 확인해야 한다. 집기 블록명 판별에도 같은 방식의 선택 훅(`LlmFixtureResolver`)이 있다.

### 2.3 사람 검토 큐

검토 큐 항목에는 층, 레이어, 추정 의미, 신뢰도, 근거, 경쟁 의미, 샘플 문자, 확인 질문이 들어 있다. 파이프라인은 검토를 기다리지 않고 추정 의미로 파싱을 계속한다. 검토 결과는 매니페스트 `layers` 매핑으로 다음 파싱에 반영할 수 있다. 단, 이 매핑은 모든 층에 같은 이름으로 적용된다. 층마다 의미가 다른 이름(예: `0`, `LAYER_17`)은 매핑하지 않는다.

## 3. 허용 오차와 매칭 기준

| 항목 | 기준 | 위치 |
|---|---|---|
| 열린 폴리라인 자동 닫기 | 끝점 간격 0.05 m 이하. 이슈 `auto_closed` | `geometry.CLOSE_TOL_M` |
| LINE 끝점 스냅 후 폴리곤화 | 0.05 m. 이슈 `polygonized_from_lines` | `geometry.SNAP_TOL_M` |
| 닫지 못한 열린 폴리라인 | 이슈 `open_polyline_unclosed`(warning) | `geometry` |
| 유효하지 않은 폴리곤 | `make_valid` 후 가장 큰 조각 사용. 이슈 `invalid_fixed` | `geometry` |
| 최소 폴리곤 면적 | 0.01 m² 미만은 버린다 | `geometry.MIN_AREA_M2` |
| 중복 폴리곤 | IoU 0.98 이상이면 하나만 남긴다. 이슈 `duplicate_removed` | `geometry.DUPLICATE_IOU` |
| 존 폴리곤 구멍 | 다른 존 폴리곤 안에 완전히 들어 있는 존 폴리곤을 바깥 폴리곤에서 구멍으로 뺀다. 안쪽 폴리곤은 별도 조각으로 유지한다. | `geometry.subtract_nested` |
| 매장 라벨 | 폴리곤 포함, 아니면 1.0 m 이내 최근접 | `labels.label_units` |
| 분리된 브랜드 문자 | 같은 폴리곤 안, 아니면 코드 문자 높이의 3배 이내 | `labels.label_units` |
| 존 라벨 | 3.0 m 이내 최근접 | `labels.label_zones` |
| 수직동선·편의시설 라벨 | 2.0 m 이내 최근접 | `labels` |
| 출입구 라벨 | 선에서 3.0 m 이내 최근접 | `labels.label_openings` |
| 집기 소속 매장 | 외곽 중심점을 포함하는 매장 → 가장 많이 겹치는 매장 → 0.5 m 이내 최근접 | `fixtures.assign_units` |
| 매장 경계 겹침 QA | 두 매장이 0.5 m² 넘게 겹치면 이슈 `unit_overlap` | `pipeline` |
| 사람 검토 기준 | 레이어 신뢰도 0.7 미만 | `classify.REVIEW_THRESHOLD` |

## 4. 엔티티와 속성

| 엔티티 | ID | 주요 속성 |
|---|---|---|
| 매장 | `U-{unit_code}` | `unit_code`, `brand_name`, `vacant`, `area_m2`, `zone_code`, `brand_id`, `category`, `frontage_m`, `geometry_source`, `label_method` |
| 집기 | `F-{fixture_id}` | `fixture_type`, `unit_code`, `width_m`, `depth_m`, `rotation_deg`, `block_name`, `id_generated` |
| 보행 통로 | `W-{floor}-{n}` | `area_m2` |
| 수직동선 | `V-{floor}-{link_id}` | `link_id`, `link_type` |
| 편의시설 | `A-{floor}-{type}-{n}` | `amenity_type` |
| 출입구 | `O-{floor}-{opening_id}` | `opening_id`, `opening_type` |
| 존 | `Z-{zone_code}` | `zone_name`, `category`, `unit_count`, `parts`, `inferred_parts` |
| 건물 외곽 | `X-{floor}` | `width_m`, `depth_m` |

- **번호 부여:** 통로 `n`은 중심점의 (y, x) 순서로, 편의시설 `n`은 유형별로 같은 순서로 붙인다. 이 번호는 정답 데이터의 번호와 다를 수 있고, 개정 간 동일성을 보장하지 않는다. 개정 비교는 통로를 ID가 아니라 기하로 비교한다.
- **집기 유형:** `FX_{TYPE}` 블록명 → `TYPE` 속성 → 블록명 키워드(`쇼케이스`, `행거_01`, `곤돌라_A`, `카운터`, `진열테이블`, `마네킹`, `피팅룸`, `식탁_4인` 등) → 선택 훅(LLM) 순서로 정한다. 판별하지 못하면 `UNKNOWN`과 이슈 `fixture_type_unknown`을 남긴다.
- **집기 ID:** `FIXTURE_ID` 속성을 사용한다. 속성이 없거나 층 안에서 중복되면 `{unit_code}-F{nnn}`을 부여한다. 번호는 그 매장에서 쓰지 않은 번호를 외곽 중심점의 (y, x) 순서로 채운다. 이런 집기는 `id_generated=true`와 이슈 `fixture_id_generated`로 표시한다.
- **브랜드 레지스트리:** 목록, DataFrame, CSV 경로(`brand_id`, `brand_name`, `category`)를 받는다. 유니코드 정규화(NFKC), 대소문자, 공백 차이만 무시하고 이름이 정확히 일치할 때 `brand_id`와 `category`를 정한다. 같은 이름이 여러 ID에 있으면 정하지 않는다. 일치하지 않으면 이슈 `brand_unresolved`를 남긴다.
- **카테고리:** 레지스트리에서 정하지 못하면 매장을 포함하는 존 라벨의 이름(카탈로그 카테고리명)으로 정한다. 둘 다 없으면 이슈 `category_unresolved`를 남긴다.
- **라벨 없는 존 조각:** 존 라벨은 가장 큰 조각에만 있을 수 있다. 라벨 없는 조각은 소속 매장의 레지스트리 카테고리 다수결이 한 존과 일치할 때 그 존에 배정한다(이슈 `zone_part_inferred`). 레지스트리가 없으면 배정하지 않는다(이슈 `zone_part_unlabeled`).
- **관계:** `CONTAINS`(존→매장, 매장→집기)는 항상 계산한다. `cad2anything.spatial.features.compute_relations`를 불러올 수 있으면 `ON_FLOOR`, `ADJACENT_TO`, `FACES`, `NEAR`, `CONNECTED_TO`를 추가한다. 불러올 수 없으면 `ON_FLOOR`만 직접 계산하고 이슈 `relations_basic`(info)을 남긴다. `relations="basic"`은 처음부터 `CONTAINS`와 `ON_FLOOR`만 계산한다.

### 4.1 파싱 보고서

`ParseReport.to_dict()`는 다음을 담는다.

- `layer_table`: 층·레이어별 의미, 신뢰도, 판정 방법, 근거, 경쟁 의미, 요소 수.
- `issues`: 심각도(info, warning, error), 코드, 한국어 메시지, 관련 엔티티 ID.
- `stats`: 층별·전체 엔티티 수, 자동 닫기·폴리곤화 수, 생성한 집기 ID 수, 한글 문자 수, 문자 복구 수, DXF 버전, 단위.
- `review_queue`: 2.3절의 검토 항목.
- `timings_s`: 단계별 처리 시간.

## 5. 개정 비교 규칙(`revision/diff.py`)

`diff_models(before, after)`는 두 파싱 결과만 사용한다. 정답 ID를 사용하지 않는다. 매장은 층마다 매장 코드와 기하로 대응시킨다.

| 변경 유형 | 판정 규칙 |
|---|---|
| SPLIT | 이전 매장 하나를 이후 매장 2개 이상이 덮는다. 각 이후 매장은 면적의 90% 이상이 이전 매장 안에 있고, 합계가 이전 매장 면적의 90% 이상이다. |
| MERGE | 이전 매장 2개 이상이 한 이후 매장 안에 각각 90% 이상 들어가고, 합계가 이후 매장 면적의 90% 이상이다. |
| 같은 매장 | 같은 코드이고 겹침이 작은 쪽 면적의 20% 이상이다. 남은 매장끼리는 IoU가 큰 쌍부터 IoU 0.5 이상이면 코드가 바뀐 같은 매장으로 본다. |
| RESIZE | 같은 매장의 면적이 0.5 m² 넘게 바뀌거나 IoU가 0.98 미만이다. 인접 매장과 통로 경계 변화를 세부 정보에 기록한다. |
| ADD / REMOVE | 이전 / 이후에 대응 매장이 없다. 그 자리의 이전 / 이후 통로·편의시설 면적을 기록한다. |
| MOVE | 브랜드가 한 매장에서 사라지고, 분할·병합 후속이 아닌 다른 매장에 나타난다. 같은 층, 가까운 중심점 순으로 짝을 짓는다. 원래 매장의 공실 전환은 MOVE에 포함한다. |
| REASSIGN | 같은 매장의 브랜드가 바뀌었고, 새 브랜드가 다른 매장에서 이전해 오지 않았다. 공실 → 입점과, 다른 곳으로 이전하지 않은 브랜드의 퇴점(→ 공실)도 포함한다. |
| FIXTURE_CHANGE | 같은 매장에서 집기 유형별 개수가 바뀌었다. 기하나 브랜드가 바뀐 매장은 제외하고, 그 집기 수는 해당 변경 항목의 세부 정보에 남긴다. |
| WALKWAY_CHANGE | 층의 통로 합집합이 1.0 m² 넘게 바뀌었다. 0.5 m² 이하의 조각은 무시한다. 바뀐 조각을 접하는 통로 폴리곤으로 묶어 묶음마다 항목 하나를 만든다. |

- **브랜드 식별:** 정규화한 브랜드명을 쓰고, 이름이 없으면 `brand_id`를 쓴다. 공실은 브랜드가 없는 상태로 본다.
- **항목 내용:** 각 항목에 이전·이후 매장 코드, 브랜드 ID, 엔티티 ID, 세부 정보(`area_before_m2`, `area_after_m2`, 집기 수 등)와 한국어 요약을 채운다.

### 5.1 비교 결과 평가

`evaluate_diff(predicted, truth)`는 변경 유형별 정밀도·재현율을 계산한다. 예측 항목과 정답 항목은 유형과 층이 같고 매장 코드 집합이 겹치면 일치로 본다. MOVE는 출발 코드와 도착 코드가 모두 겹쳐야 한다. WALKWAY_CHANGE는 매장 코드가 없으므로 유형과 층만 비교한다. 이렇게 하면 정답이 항목을 묶는 방식이 예측과 달라도 평가할 수 있다. 코드 집합까지 같은 항목 수는 `exact_truth_matches`로 따로 보고한다.

## 6. 실행 방법

```powershell
uv run python -m cad2anything.cadcore.pipeline --rev-dir data/generated/drawings/R1/messy `
    --out data/generated/parsed/R1_messy.json --brands data/generated/business/brands.csv [--llm]
uv run python -m cad2anything.revision.diff --before data/generated/parsed/R0_messy.json `
    --after data/generated/parsed/R1_messy.json --out data/generated/parsed/changes_R0_R1_messy.json
```

- 파이프라인 CLI는 모델 JSON(`--out`)과 보고서 JSON(기본값 `<out>.report.json`)을 쓴다. `--truth`에 정답 배치 JSON을 주면 `compare_with_truth` 지표를 출력한다.
- 비교 CLI의 `--truth`에 정답 변경 JSON을 주면 `evaluate_diff` 지표를 출력한다.
- `--llm`은 저장소 루트 `.env`의 `FOUNDRY_PROJECT_ENDPOINT`와 `MINI_MODEL_DEPLOYMENT`, 그리고 Azure CLI 자격 증명을 사용한다.

## 7. 테스트 기준과 한계

`tests/test_cadcore.py`와 `tests/test_revision_diff.py`는 합성 데이터(seed 42)를 임시 폴더에 생성해 다음 기준을 확인한다.

| 대상 | 기준 |
|---|---|
| clean R0 | 매장 정밀도·재현율 0.99 이상, 면적 상대 오차 중앙값 0.5% 미만, 브랜드명 정확도 0.99 이상, 집기 매칭 0.98 이상, 층마다 수직동선 6개 |
| messy R0 | 매장 재현율 0.95 이상, 면적 오차 중앙값 1% 미만, 브랜드명 정확도 0.95 이상, 한글 복원, LLM 없이 잡음이 아닌 레이어의 90% 이상 정답, 0.7 미만 레이어의 검토 큐 등록, 가짜 LLM 응답으로 검토 큐 해소 |
| clean 비교(R0→R1, R1→R2) | 전체 재현율 0.9 이상, 두 비교에서 9개 유형 모두 검출, 변하지 않은 매장에 MOVE·REASSIGN 없음 |
| messy 비교(R0→R1) | 전체 재현율 0.8 이상 |

실제 Foundry LLM 테스트는 환경 변수 `CAD2ANYTHING_LIVE=1`일 때만 실행한다.

알려진 한계는 다음과 같다.

- 매장 코드, 존 코드, 수직동선 ID의 정규식은 이 저장소의 규약을 기준으로 한다. 다른 코드 체계의 도면에는 패턴을 추가해야 한다.
- 문자 중심점은 글자 폭 추정에 의존하므로, 라벨이 경계 가까이 있으면 다른 공간에 배정될 수 있다.
- 블록 안에 중첩된 INSERT는 펼치지 않는다.
- 브랜드 레지스트리는 정확한 이름 일치만 사용한다. 표기 변형(약칭, 오타)은 `brand_unresolved`로 남는다.
- 생성한 집기 ID는 원래 ID와 다를 수 있다. 개정 비교는 집기 ID가 아니라 유형별 개수로 판단한다.
