# KEV-MAPF 구현 현황과 시나리오 변경 안내

기능 사용은 [일반 주문 연결과 실행 설정](order-traffic-integration.md), 검증 범위는 [전체 비교 결과](experiments/order-traffic-results-and-coverage-20261024.md)를 함께 읽는다. 개인 채팅 인수인계는 기능 공유에서 제외하고 로컬에 보관한다.

전체 흐름과 목표부터 보려면 [전체 설계와 목표](continuous-order-traffic-design.md)를 먼저 읽는다.

최종 정리일: 2026-10-24 (한국 시간). 초기 작성·시험 수치는 아래에서 당시 기록으로 구분한다.
작업 브랜치: `woozoo`. 저장소 위치는 각 PC의 체크아웃 경로를 사용한다.
초기 코드 비교 기준은 `e6899d4`이며, 기능 공유 정리는 `3e26822` 이후 작업 파일을 기준으로 한다.

이 문서는 이번 작업 전에 있던 구성, 이번에 추가한 구성, 변경된 실행 흐름을 구분한다.
공유 파일 링크는 저장소 상대 경로를 사용한다. 실행 기록과 제외한 과거 진단 도구는 로컬 보관 경로로 표시한다.

설계의 [시간 지표 정의](continuous-order-traffic-design.md#traffic-time-metrics),
[상황·후보·선택 방식 정의](continuous-order-traffic-design.md#traffic-decision-terms),
[대기·실행 결과 정의](continuous-order-traffic-design.md#traffic-decision-outcomes)를 공통 기준으로 쓴다.
후보 선택의 주 지표는 **평균 이동 완료 시간(실행 기준)**, 판단 비용까지 포함한 지표는 **평균 요청 처리 시간(판단 포함)**이다.
아래의 초기 시험 기록에 있는 전체 완료 소요 시간은 이 두 평균과 구분한다. 과거 로그 숫자는 그대로 보존한다.

## 전략부터 제안하는 세 방식 비교 — 2026-10-23

사용자 승인으로 [코드 탐색·KEV 전략 탐색·jhlee 전략](experiments/traffic-strategy-three-way-20261023.md)을 별도 실행 도구에 구현했다. 코드·KEV는 같은 탐색기에서 다음 전략 순서만 바꾸며, 전략을 고른 뒤 기존 A*로 필요한 우회 경로를 만든다. jhlee는 고정 원본 판단 엔진을 공통 A*·예약 검사에 연결한다. 현재 노드 대기·목적지 이동·우회만 허용하며 다중 양보·동시 주행·일반 주문 처리를 포함하지 않는다.

5상황을 세 방식으로 각각 1회, 총 15회 실행했다. 정상 4상황의 완료는 코드 4/4, 새 KEV 탐색 4/4, jhlee 2/4다. 유일한 출구가 막힌 대조는 모두 출발하지 않았다. 세 대 출발의 예상 평균은 코드 19.327초, 새 KEV 탐색 23.508초로 KEV가 느렸다. jhlee의 미해결은 호출 8회 한도와 정차 로봇을 기다리는 제안의 예약 검사 탈락으로 구분한다. 실제 모델 호출은 24회, 공통 준비 1회다.

새 자동 검사 15개·전체 검사 296개 통과, 실행한 계획 10개의 점유 표본 2,905회에 중복 점유가 없었다. 기존 일반 주문·화면·설정은 바꾸지 않았고 시험용 8012 모델 서버는 종료했다. 이번 조건에서 KEV 이점을 얻지 못했으므로 추가 모델 시험·운영 연결을 자동으로 진행하지 않는다.

사용자 승인 후속은 `--profile equal_time`이다. 총 모델 호출 상한만 제거하고 같은 10초·검사·우회 한도와 입력을 사용한다. 5상황×3방식 1회씩의 결과는 정상 완료 수가 같고, 세 대 출발에서 코드 예상 평균 19.327초·KEV 20.143초였다. KEV는 이 상황에 25회, jhlee는 9회 호출했으며 jhlee 완성 제안은 예약 검사에서 탈락했다. 후속 점유 표본 2,838회 중복 없음, 새 검사 18개·전체 검사 299개 통과. 최초 8회 상한 시험과 별도 보관하며 지시문·모델을 바꾼 시험은 아니다. 실제 관제에 세 판단기를 동시에 붙인 상태도 아니다.

사용자 승인으로 [같은 상태의 확률 순서 재사용 비교](experiments/traffic-strategy-probability-order-20261023.md)를 추가했다. 같은 5장면에서 기존 KEV와 최종 계획을 유지하며 모델 호출 합계가 30회→11회로 줄었다. 세 대 출발은 25회→8회, 판단 약 5.67초→1.72초였다. 코드보다 좋은 계획을 찾은 결과는 아니며 일반 주문·화면에는 연결하지 않았다. 관련 회귀 검사 238개 통과, 별도 평가 도구 2파일의 지도 입력 오류로 프로젝트 전체 검사 수집은 중단된 상태다.

## 현재 관제 목록과 일반 주문의 연결 범위 — 2026-10-21 정리

현재는 사용자 결정으로 추가 jhlee 비교를 보류하고 우리 방식의 연속 처리 점검으로 돌아왔다. [새 KEV 전략 순서·무작위 주문 12건](experiments/our-strategy-continuous-validation-20261023.md)은 물품 33개·작업·홈 복귀 모두 완료, 주문·복귀·모델 오류 0, 모델 요청·응답 19/19, 점유 표본 237회 중복 없음이었다. 기존 UI·제품 코드·운영 기본값은 바꾸지 않았다. KEV가 코드보다 더 좋은 계획이나 더 적은 탐색을 제공한다는 증거는 여전히 별도 과제다.

이후 [공급 중지 후 완료·복귀 표시](experiments/order-stream-stop-status-fix-20261023.md)를 수정했다. 상태 조회에서 해당 공급 주문의 최신 집계를 반영하며, 공급 중지·실패 상태와 원래 이유는 유지한다. 저장된 12건 결과의 API 확인과 관련 검사 272개를 통과했고, 기존 완료 6건·로봇 위치를 보존해 화면용 시뮬 백엔드에 반영했다. 배차·KEV 판단 규칙은 그대로다.

10월 23일 최신 [jhlee 일반 주문 별도 연결·최종 비교](experiments/jhlee-order-integration-20261023.md)를 추가했다. 원본 Active/Pending·게이트·판단 엔진을 유지하고 A* 연결의 점유 회피 입력을 보완했다. 단독·두 건은 완료했으나 최종 같은 6건에서는 코드·우리 새 KEV 각각 6/6, jhlee 2/6 완료 뒤 주문 대기 한도 실패였다. 같은 BLOCKED 분석 반복으로 요청 시도 348회·응답 347회, 엄격한 요청 카운터 대조 1회 불일치도 남겼다. 원본 소스·실패·모델 응답을 보존하고 자동 추가 시험은 하지 않았다. 관련 검사 270개 통과. 새 연결은 별도 `jhlee_order_preview` 시험 프로세스에만 있으며 현재 화면의 정책 목록과 일반 서버 기본값은 유지한다.

10월 23일 최신 [동일 일반 주문의 세 방식 비교](experiments/order-traffic-policy-comparison-20261023.md): 코드 선택·기존 KEV 최종 선택·새 KEV 전략 순서를 각각 새 시뮬 서버에서 6건·8개로 한 번씩 실행했다. 모두 작업·홈 복귀 완료, 20개 적용 경로 기록은 같았고 전체 작업 끝은 53.41·53.33·54.14초, 모델 호출은 0·1·9회였다. 새 방식은 검사 전에 물으므로 9회 중 8회는 검사 뒤 유효 경로가 하나만 남았다. 이번 조건의 이점은 확인하지 못했고 jhlee는 이 일반 주문 비교에 포함하지 않았다. 총 870회 점유 표본 중복 없음, 관련 검사 256개 통과. 현재 화면·주문은 유지하고 별도 비교용 자식 서버만 종료했다.

10월 23일 후속 [일반 주문 전략 순서 연결](experiments/order-traffic-strategy-integration-20261023.md)을 추가했다. 시뮬·AI의 새 이동 요청에서 KEV가 생성 전 경로 전략 순서를 제안하고, 코드는 최신 예약 검사 후 새 요청의 예상 완료 시간이 짧은 경로를 고른다. 기존 두 방식과 함께 화면에서 선택하며 작업·복귀 중 변경은 거부한다. 별도 시뮬 서버의 일반 주문 6건·8개를 모두 완료했고 20회 이동 적용 중 전략 호출은 9회였다. 정지 배치의 전체 로봇 DFS를 일반 주문에 그대로 연결한 것이 아니며, 다중 양보·실기체·기존 주행의 재계획 범위는 유지한다.

10월 23일 별도 [확률 재사용의 새 네 장면 비교](experiments/traffic-strategy-probability-new-cases-20261023.md)는 일반 주문 연결 범위를 바꾸지 않는다. 세 방식 모두 4/4 완료, 두 KEV 방식의 최종 계획 동일, 모델 호출 합계 51회→15회였다. 새 시험 정의와 문서만 추가했으며 탐색기·관제 코드는 수정하지 않았다.

**화면은 9개로 줄였다.** 일반 주문 공급 1개, 경로 연결 1개, 교통조정 기본 판단 4개, 여러 대 양보 비교 3개다. 예전·중복 14개는 `개발용 보관`으로 숨기고 기존 API·CLI·자동 검사용으로 유지했다. 원래 21개에 현재 비교 조건을 맞춘 `dependency_code_two_robot_yield`, `dependency_kev_two_robot_yield` 두 항목을 추가해 파일에는 총 23개가 있다.

새 비교는 기존 혼합 항목 `guided_deferred_two_robot_yield`와 같은 지도·양보 경로·관계 정보·분기 보관·128회 검사/8단계/10초를 사용한다. KEV 두 항목은 8회 제안/개별 응답 2초도 같다. 예전 DFS·KEV_GUIDED·고정 계획·BFS는 현재 공정 비교 목록에서 제외했다. [현재 9개와 보관 이유](traffic_scenarios.md)를 기준으로 선택한다. 아래 표의 초기 항목 수와 이름은 개발 당시 기록이다.

일반 주문은 **다른 로봇의 기존 주행·예약을 유지하고 새 이동 요청의 경로를 조정**하는 데 연결했다. **새 여러 대 양보 → 통과 → 복귀는 별도 배치 시뮬레이션까지 확인했으며, 일반 주문 중 자동 복구나 실기체 연결은 아직 하지 않았다.** 이번 목록 정리는 그 실행 범위를 바꾸지 않는다.

## 여러 대 양보의 거리·시간 입력 보완 — 2026-10-22

이제 별도 비교 세 항목은 경로·점유·누가 길을 막는지와 함께 **지도에서 계산한 거리, 이동 예상 시간, 남은 경로의 낙관적인 도착 시간**을 받는다. `traffic_recovery_motion.py`에서 계산하고, 관계 입력과 선택지 설명에 같은 값을 넣는다. 코드 비교도 같은 정보를 사용한다. 지도 전체 연결표나 상세 예약 시간표를 모델에 새로 주는 것은 아니다.

예: R2 N11→N6은 약 0.38m / 1.89초, R3 N10→N8은 약 0.46m / 2.29초다. 짧다고 무조건 선택하지 않으며 길막 관계·기존 예약 검사·완성 계획 확인을 계속 거친다. 누가 먼저 비켜줘야 하는지는 거리만으로 알 수 없다.

실제 KEV로 24회 계획 시험·108회 모델 호출을 수행했다. 두 대 양보의 KEV 단독 성공은 보완 전후 모두 0/3이고, 혼합 방식은 코드 보완으로 3/3 해결했다. 우회가 있는 한 상황에서는 같은 최종 경로를 찾는 호출이 5회→1회로 줄었으나 한 번의 탐색 결과다. [조건·입력 예시·수치·한계](experiments/kev-mapf-recovery-motion-input-20261022.md)를 함께 읽는다.

일반 주문과 실기체에는 이번 보완을 적용하지 않았다. 탐색은 여전히 처음 찾은 완성 계획을 실행하며 전체 시간 최적화가 아니다.

같은 날 [후속 실패 기록 분석](experiments/kev-mapf-motion-answer-analysis-20261022.md)도 수행했다. 새 모델 호출·관제 명령 없이 당시 제공된 선택에서 끝까지 해결 가능한지 확인했다. 막힌 출발·조기 복귀·해결 가능한 순서 미루기가 반복됐다. 실행 코드나 현재 선택 방식을 추가 변경하지 않았다.

후속 [짧은 입력 48회 비교](experiments/kev-mapf-concise-probe-20261022.md)는 핵심 상태마다 6회 중 5회 이상이라는 사전 기준에 미달해 중단했다. 두 대 모두 비킨 상태에서도 짧은 설명 KEV는 0/6이었다. 추가 전체 시험·지시문 재수정은 하지 않았고, 운영 설정도 그대로다. 현재 다중 양보 순서를 KEV에게 더 맡기는 확장은 권하지 않는다.

후속 [기능 동작과 KEV 효과 구분·코드 기준 12건 시험](experiments/kev-mapf-evidence-status-20261022.md): 일반 주문의 코드 선택으로 12건·27개를 작업·복귀까지 완료했다. 748회 표본에서 중복 점유가 없었고 새 모델 호출은 0회다. 종료 후 기존 KEV 선택 설정과 원래 위치를 복원하고 시험 이력을 보존했다. 이 결과를 KEV 처리량 개선으로 해석하지 않는다. 일반 주문의 KEV 선택 오류·경로 생성 실패도 문서에서 별도로 구분했다.

후속 [예약 대기 중 취소와 같은 로봇 재사용](experiments/reservation-wait-cancellation-20261022.md): 통로가 비어도 취소 주문이 재출발하지 않고 새 주문은 같은 R1이 작업·복귀까지 완료했다. 317회 표본에 중복 점유가 없었다. 최초 관측기 오류로 대기 한도에 도달한 실패 주문 1건도 보존해 재시험과 구분했다. 관제 실행 코드 변경·새 모델 호출 없이 원래 설정·위치를 복원했다.

후속 [출발 대기 한도·실패 후 명시적 재시도](experiments/order-timeout-retry-20261022.md): 새 시험 주문 1건에서 약 60.04초 뒤 실패·복귀 완료 처리, 통로 해제 후 실패 유지, 재시도 요청 후 R2의 작업·복귀와 실패 이력 보존을 확인했다. 687회 표본에서 중복 점유 없음. 실행 코드 변경·모델 호출 없이 원래 설정·위치를 복원했다. 홈 밖에서의 복귀 길 장기 차단 시험은 아니다.

최신 [홈 밖 복귀 차단·재사용](experiments/blocked-return-validation-20261022.md): N3의 R1이 복귀를 약 180.09초 기다린 뒤 BLOCKED로 정지했고 새 배차에서 제외됐다. R2는 대기 중·BLOCKED 뒤 주문 2건을 완료했다. 홈 밖 재시도는 거부하고, 운영자 통로 해제·실제 홈 복귀 후에는 원래 주문 재시도와 R1의 새 주문 처리가 완료됐다. 본 시험 1,748회·별도 재사용 155회 표본에서 중복 점유 0. 신규 4건 모두 완료, 모델 요청·실행 코드 변경 없이 원래 설정·위치·기존 16건을 보존했고 최종 이력 20건이다. 연결·위치 정보 이상 시험은 후속 제안이며 필수 다음 작업으로 정하지 않았다.

## 일반 주문 구현의 현재 위치 — 2026-10-25

병합·재설계에 넘길 일반 주문용 핵심은
[`backend/app/services/mapf/README.md`](../backend/app/services/mapf/README.md)부터 확인한다.
교통조정 진행·KEV 전략 순서·기존 경로 생성기 연결·공통 함수만 이 폴더로 옮겼다.
필요한 기존 FMS 변경분은 같은 폴더의 `integration/existing-fms-support.patch`에 검토용으로 보관했다.
기존 경로 세 파일은 호환 연결로 남겼다. 별도 연구·시험 파일과 과거 실험 소스 사본은 이동하지 않았다.
동작·기본 설정·KEV의 판단 역할은 유지했으며, 아래 과거 개발 기록의 파일 경로와 구분한다.

## 1. 먼저 구분할 시험

**최신(2026-10-24):** [보통 주문과 중간 폭증](experiments/order-traffic-ordinary-burst-20261024.md)은 별도 배치 도구가 코드·지도 223개 파일을 고정하고 8035 시뮬 서버·8012 전용 KEV만 관리했다. 사용자 관제에는 조회만 했고 사용자 모드·주문 변화는 관측 정보로 남겼다. 같은 품목·수량·순서를 보통 접수/중간 폭증으로 공급해 기존 알고리즘과 우리 AI를 총 8회·96건 비교했다. 300개 모두 작업·홈 복귀 완료, 주문·복귀·모델 오류 0, 점유 표본 5,103회 중복 없음, 모델 요청/응답 85/85회, 관련 검사 307개 통과. 시험 서버만 종료했다.

보통 두 경우와 폭증42는 AI가 조금 느렸고 폭증137은 AI가 평균 3.72초 빨랐다. 후자는 배차 로봇·작업대가 달라졌고 두 선택 모두 코드가 맡는다. AI 이동 160회·검사 묶음 190회에서 준비 전략을 모두 시도했으므로 KEV의 검사량 절감 근거는 없다. 정상 처리와 AI 판단의 기여를 구분하며, 병행 작업이 있는 한 번씩의 비교로 성능 우위를 확정하지 않는다.

**현재 최종 비교 대상(2026-10-24):** 먼저 **시뮬·알고리즘의 기존 경로·예약 제어**와 **시뮬·AI의 A*＋우리 KEV 전략 교통조정**을 비교한다. 검증 후 실제·알고리즘/실제·AI로 옮긴다. 이전 `MIN_EXPECTED_TIME`은 추가 교통조정 내부의 코드 선택 조건이며 화면의 기존 알고리즘 모드가 아니다. 두 비교를 구분한다. [조건·실행 기록](experiments/order-traffic-native-mode-comparison-20261024.md).

새 [두 모드 비교 도구](../tests/order_traffic_native_mode_comparison.py)는 같은 시뮬 전용 진입점·주문 관측기를 쓰고 시작 모드와 주문 모드만 각 조건에 맞춘다. 기존 알고리즘은 추가 교통조정 호출 없이 원래 `navigate_to_node(..., required_path=None)`로 들어가고, 우리 AI는 A* 경로＋`KEV_STRATEGY_ORDER`를 거친다. 모드 응답·실제 저장 주문·추가 판단 로그·모델 카운터로 분기를 확인한다. 별도 서버에서 실행하며 현재 관제와 실기체에는 명령하지 않는다.

첫 혼합 12건씩은 두 모드 모두 작업·복귀 완료, 평균 주문 시간 54.25초/50.39초, 홈 복귀까지 141.25초/128.79초, 모델 요청 0/19회였다. 점유 관측 1,232회 중복 없음, 관련 검사 301개 통과. 기존 화면 선택 모드의 AI→알고리즘 변화가 감지돼 전체 보존 검사 한 항목은 불일치로 남겼다. 주문·위치·시나리오는 동일하며 원문·실행 상태를 성공으로 덮어쓰지 않았다. 한 번의 시뮬 비교로 일반 우열·실기체 준비 완료를 확정하지 않는다.

최신 [전략 순서·응답 대기 대조](experiments/order-traffic-ranking-ablation-20261023.md)는 일반 서비스 소스를 바꾸지 않고 별도 시뮬 진입점에서 제안기만 교체했다. 같은 12건씩 새 KEV·고정 순서·고정 순서＋평균 대기를 실행해 36건 모두 완료했다. 최종 복귀는 128.79·141.37·128.67초로, 모델 없는 대기 대조군도 빠른 흐름을 재현했다. 실제 모델 요청 19회·점유 표본 1,821회 중복 없음·관련 검사 293개 통과. 새 KEV와 대기 대조군의 배차 로봇은 전부 같고 경로도 47/48개 같았다. 이번 장면에서 모델 전략 제안의 독립적 성능 이점은 입증하지 못했다. 별도 고정 제안 기록은 실제 모델 요청과 구분하며 일반 관제·실기체·운영 대기는 추가하지 않았다.

후속 [연속 주문 부하 비교](experiments/order-traffic-load-comparison-20261023.md)는 A/B 집중·두 픽업대 혼합·빠른 공급의 두 주문표씩을 코드 선택과 새 KEV 전략 순서에 동일하게 전달했다. 기존 비교 도구에 외부 주문표·시험 한도·대기 집계 옵션을 추가했고 별도 실행 도구가 seed와 정책 순서를 관리한다. 12회 운행·144건 작업/복귀 완료, 모델 요청/응답 116/116·오류 0, 점유 표본 7,233회 중복 없음, 관련 검사 281개 통과. A/B는 코드가 빠르고 혼합·빠른 공급은 새 KEV가 빨랐지만 배차 로봇·경로 변화가 함께 있어 AI 판단 단독 효과로 해석하지 않는다. 작업대는 모두 N3으로 같았다. 일반 관제 실행 코드·기존 화면 상태는 유지했다.

[저장 로그 분기 분석](experiments/order-traffic-load-review-20261023.md)은 표준 라이브러리만 쓰는 별도 분석 도구로 원문 49개를 읽는다. 혼합·빠른 접수에서 첫 차이는 6번 주문의 R3/R2 배차였고, 새 KEV의 264회 이동 모두 전략을 끝까지 시도하고 코드 규칙으로 최종 선택했다. 추가 운행·모델 요청 없이 분석 관련 검사 19개 통과, 실행 코드는 유지했다. 이후 위의 별도 운행 대조에서 평균 대기 대조군도 빠른 흐름을 재현했다.

| 종류 | 기존/추가 | 정의 파일 | 실행·관리 | 무엇을 확인하는가 |
| --- | --- | --- | --- | --- |
| 일반 지도 이동 시나리오 8개 | 기존. 이번에 AI 모드 경로 생성 연동을 보완 | [traffic_scenarios.json](../tests/traffic_scenarios.json) | [scenario_service.py](../backend/app/services/scenario_service.py), 기존 화면·CLI | 지정한 시작점에서 목적지까지 기존 FMS로 이동 |
| KEV-MAPF 지도 시나리오 7개 | 이번에 추가. 기존 목록과 실행 진입점을 재사용 | 같은 `traffic_scenarios.json` | 기존 관리자의 새 분기 → [traffic_joint_action_scenario.py](../backend/simulation/traffic_joint_action_scenario.py) | 입력 구성 → 조정안 생성·검사 → KEV 선택 → 가상 주행 |
| 여러 로봇 양보 시험 2개 | 2026-10-20 추가. 고정 계획 / 코드 자동 탐색 | 같은 `traffic_scenarios.json`의 `fixed_two_robot_yield`, `search_two_robot_yield` | 같은 관제 실행기와 [traffic_recovery_search.py](../backend/app/services/traffic_recovery_search.py) | 5단계를 직접 준 동작 시험과, 제공한 경로에서 순서를 자동으로 찾는 시험. 둘 다 KEV 호출 없음 |
| 전략 탐색 비교 2개 | 2026-10-20 추가. 코드 DFS / KEV 제안 DFS | 같은 `traffic_scenarios.json`의 `dfs_two_robot_yield`, `guided_two_robot_yield` | 공통 탐색기와 [traffic_recovery_guidance.py](../backend/app/services/traffic_recovery_guidance.py) | 같은 동작·검사·한도에서 다음 동작 선택만 비교. 모델 오류·한도·취소에는 이동하지 않음 |
| 일반 주문 다중 등록 시나리오 2개 | 이번에 추가 | [order_scenarios.json](../tests/order_scenarios.json) | [order_scenarios.py](../tests/order_scenarios.py), 기존 주문 API | 여러 주문의 등록·배차·픽업·작업대·후속 주문 처리 |
| 관제의 연속 주문 공급 1개 | 1단계로 추가 | 같은 `traffic_scenarios.json`의 `continuous_ai_orders` | 기존 관리자의 주문 공급 분기 → [order_stream_scenario.py](../backend/simulation/order_stream_scenario.py) | 화면에서 공급 시작·중지, 대기열 상한, 일반 주문의 연속 처리 |

따라서 앞서 사용한 표의 “기존 지도·KEV-MAPF 테스트”는 두 종류를 묶은 표현이었다.
**지도 테스트 틀과 8개 시나리오는 기존이고, 그 틀에 넣은 KEV-MAPF 7개는 새로 만든 것이다.**
정해진 6건을 넣는 CLI 시험은 별도로 유지하고, 계속 공급하는 항목은 기존 드롭다운에 추가했다.

2026-10-20의 추가 시험은 [두 로봇 양보 순서 자동 탐색](experiments/two-yield-search-20261020.md)을 따른다.
자동 탐색은 횟수·단계·시간 한도 안에서 실행 가능한 순서를 찾는 단계이며, 평균 시간 최적화는 아직 하지 않는다.
후속으로 [KEV의 다음 검사 동작 제안·실패 피드백](experiments/kev-guided-recovery-20261020.md)을 별도 시험에 추가했다.
일반 주문에 여러 로봇의 양보를 자동 적용하지 않았다.

현재 다중 양보 배치는 **R1 작업대 N3→N0 복귀, R2=N11, R3=N10**으로 바꿨다.
[새 배치와 입력 표현·후보 순서 진단](experiments/workstation-return-input-diagnosis-20261020.md)에 이전 기록과 새 결과를 구분했다.
후속 [후보 이름·표시 순서 분리 시험](experiments/candidate-id-order-diagnosis-20261020.md)에서는 순서 영향이 재현됐지만 전체 탐색은 개선되지 않았다. 이름·순서 기본값과 일반 주문 흐름은 유지했다.
[저장 답변의 해결 가능성 분석](experiments/recovery-answer-analysis-20261020.md)에서 막힌 출발과 조기 복귀를 구분했다. 후속 [관계 정보·계획 분기 되돌아가기](experiments/dependency-recovery-20261020.md)는 별도 설정으로 구현했다. 관계 코드 5회 검사로 해결, 새 KEV는 유효 분기를 포기하거나 호출 한도에 도달해 미해결이었다. 일반 주문·기존 시나리오 기본값은 유지했고 작업 전 복원 지점을 보관했다.
이후 [미룬 순서 보관·한도 뒤 코드 보완](experiments/deferred-recovery-20261020.md)을 구현했다. 관제의 **KEV 추천 + 코드 보완** 별도 항목에서 모델 제안 8회·코드 보완 선택 4회·예약 검사 10회 후 5단계 이동을 완료했다. KEV 단독 성과와 구분하며 일반 주문 기본값은 그대로다.
2026-10-21에는 [대표 상황과 비교 기준 초안](experiments/kev-mapf-benchmark-plan-20261021.md)을 별도 준비했다. `tests/traffic_recovery_benchmark_cases.json`의 6상황을 `backend/simulation/traffic_recovery_benchmark.py`로 코드 준비 확인했다. 관제 드롭다운·주문 흐름 변경이나 새 KEV 호출은 없으며, 운영상 정차·양보 위치의 허용 여부를 확인할 단계다.

## 2. 기존 파일 중 재사용한 것

아래 파일들은 이번 작업 전에 있었으며 이번 기능에서 재사용한다. 이후 수정한 파일은 3절에 구분한다.

| 기존 파일 | 재사용하는 역할 |
| --- | --- |
| [order_manager.py](../backend/app/services/order_manager.py) | 주문 대기열·배차·처리 상태 관리 |
| [order_dispatcher.py](../backend/app/services/order_dispatcher.py) | 배차 가능한 로봇 목록과 기본 우선순위 |
| [order_route_dispatcher.py](../backend/app/services/order_route_dispatcher.py) | 일반 주문의 Algorithm/AI 경로와 기존 주행기 연결 |
| [llm_route_preview.py](../backend/app/services/llm_route_preview.py) | 기존 KEV 등 AI 경로 생성과 경로 검증 |
| [kev_selector.py](../backend/app/services/route_selector/kev_selector.py) | 실행 중인 KEV 서버에 요청하는 프로젝트의 클라이언트 |
| [route_service.py](../backend/app/services/route_service.py) | `plan_route()`의 A* 경로 계산 및 공통 검증 |
| [traffic_manager.py](../backend/app/services/traffic_manager.py), [reservation.py](../backend/app/services/reservation.py) | 기존 교통제어, 노드·물리 엣지 예약·진입 검사 |
| [mode_service.py](../backend/app/services/mode_service.py) | 실제/시뮬레이션, Algorithm/AI 모드와 기능별 처리 방식 |
| [routers/scenarios.py](../backend/app/routers/scenarios.py) | 기존 목록·실행·상태 API |
| [command_service.py](../backend/app/services/command_service.py) | 기존 실제/가상 이동·정지 호출 |
| [simulation/scenarios.py](../backend/simulation/scenarios.py) | 가상 로봇의 시나리오 시작 위치 복원 |
| [traffic_scenarios.py](../tests/traffic_scenarios.py) | 기존 지도 시나리오 API 호출 CLI |

프런트엔드의 기존 테스트 선택·실행 UI와 일반 주문 등록 API도 재사용한다.
이번 작업으로 일반 주문의 배차 정책이나 로봇 속도, OMX 방문 순서를 바꾸지 않았다.
**프로젝트의 KEV 경로 생성은 원래 구현되어 있었다. 이번에 새로 만든 핵심은 교통조정 입력·조정안 선택·시험 연결이다.**

## 3. 기존 파일에서 바꾼 것

런타임·시나리오 설정에서 기존 파일을 수정한 곳은 다음과 같다. 문서 보완은 별도다.
기존 [traffic_scenarios.md](traffic_scenarios.md)에도
새 시험의 종류·허용 모드·이 문서로의 안내를 추가했다.

| 수정 파일 | 원래 동작 | 이번 변경 |
| --- | --- | --- |
| [scenario_service.py](../backend/app/services/scenario_service.py) | 기존 지도 이동 시나리오 검증·초기 배치·이동·완료 관리 | KEV-MAPF 전용 실행 분기, 시작 전 자원 검사, 실행 모드 기록, 일반 지도 시나리오의 AI 경로 생성 연동 |
| [traffic_scenarios.json](../tests/traffic_scenarios.json) | 일반 지도 이동 시나리오 8개 | 기존 8개 정의를 유지하고 KEV-MAPF 7개·연속 주문 공급 1개 추가 |
| [order_manager.py](../backend/app/services/order_manager.py) | 지도 시나리오 실행 중 주문 등록·배차 중단 | 주문 공급 시나리오에 한해 기존 등록과 배차 루프 허용. 배차 정책·작업 순서는 유지 |
| [routers/scenarios.py](../backend/app/routers/scenarios.py) | 목록·시작·상태 API | 주문 공급 중지 API, 경로 출처 선택, 시험 그룹·목적 설명 추가 |
| [ScenarioControls.tsx](../frontend/src/components/ScenarioControls.tsx), [fmsApi.ts](../frontend/src/api/fmsApi.ts) | 기존 테스트 선택·실행·조회 | 공급 중지 버튼, 경로 생성 선택, 시험 그룹 구분과 쉬운 설명 추가 |

`ScenarioManager`의 주요 변경:

- KEV-MAPF 항목은 실제 로봇 모드에서 초기 배치 전에 거부한다.
- 고정 경로 4개 항목도 시뮬·AI에서 허용한다. 경로 출처는 시험용 고정 경로로 표시하고, 교통조정 선택기는 KEV를 유지한다.
- KEV-MAPF 항목의 기존 예약·보호 자원은 초기 배치 전에 확인한다. 초기 배치가 예약을 지워 입력 문제를 숨기지 않도록 한다.
- `traffic_joint_action` 설정이 있으면 새 가상 실행기로 넘긴다.
- 일반 지도 이동 시나리오도 AI 모드라면 기존 KEV 경로 생성 함수를 호출한다.
- 일반 지도 AI 시험은 모든 대상 경로를 생성·상태 검사한 후 `required_path`로 기존 주행기에 전달한다.
- AI 경로 생성 실패 시 A* 경로로 대체하지 않고 실패 원인을 표시한다.
- 주문 공급 시나리오는 위치 복원 없이 기존 주문 처리기로 전달한다. 직접 이동 시험과 구분해 배차 루프가 계속 돌도록 한다.
- 공급 중지는 접수 주문을 유지하며, 비상정지는 이 시험의 미배정 주문을 취소해 재배차를 방지한다.

기존 8개는 `bottleneck`, `head_on`, `cycle`, `cycle_reverse_order`,
`central_head_on`, `junction_contention`, `swap_with_crossing`, `central_cycle`이다.
현재 HEAD와 비교해 이 8개의 JSON 정의가 유지된 것을 확인했다.

## 4. 새로 만든 파일

### 입력·교통조정·실행

| 새 파일 | 역할 |
| --- | --- |
| [traffic_situation_adapter.py](../backend/app/services/traffic_situation_adapter.py) | 로봇별 경로 DTO와 전체 FMS 상태를 합쳐 TrafficSituation 입력 구성. 지원하지 않는 상태는 보류 |
| [traffic_joint_actions.py](../backend/app/services/traffic_joint_actions.py) | 입력 검증, JointAction 생성·사전 검사, KEV 선택, 선택 결과 재검사 |
| [traffic_metrics.py](../backend/app/services/traffic_metrics.py) | 순차 후보의 예상 거리·시간, 실제 단계 시각·미완료 기록. 통행 허가와 분리 |
| [traffic_joint_action_scenario.py](../backend/simulation/traffic_joint_action_scenario.py) | 기존 관제의 가상 로봇에 연결해 조정안을 단계별 실행하고 상태·로그 기록 |
| [simulation/traffic_joint_actions.py](../backend/simulation/traffic_joint_actions.py) | 관제의 로봇과 분리된 Mock 실행기와 테스트 선택기. 격리된 계약·실행 시험 |
| [traffic_selection_comparison.py](../backend/simulation/traffic_selection_comparison.py) | 같은 입력·후보로 요청 순서/계산 최솟값/실제 KEV를 반복 비교. 현재 관제 주문과 분리 |
| [order_stream_scenario.py](../backend/simulation/order_stream_scenario.py) | 무작위 1~5개 주문 생성, 공급 간격·대기열 상한, 중지·오류 처리·로그 |

### 시나리오 데이터와 자동 검사

| 새 파일 | 역할 |
| --- | --- |
| [traffic_situation_cases.json](../tests/traffic_situation_cases.json) | 격리 시험용 지도·교통 상황 5개. 관제의 실제 지도와 구분 |
| [order_scenarios.json](../tests/order_scenarios.json) | 주문 6건 연속 등록·추가 유입 일정 |
| [order_scenarios.py](../tests/order_scenarios.py) | 기존 주문 API에 입력을 등록하고 상태 조회. 기본 실행은 미리보기, `--run`으로 등록 |
| [test_traffic_joint_actions.py](../tests/test_traffic_joint_actions.py) | 조정안·사전 검사·격리 실행 검사 |
| [test_traffic_metrics.py](../tests/test_traffic_metrics.py) | 순서별 평균, 정차 로봇 제외, 양보 복귀 비용, 미완료·판단 시간 구분 검사 |
| [test_traffic_selection_comparison.py](../tests/test_traffic_selection_comparison.py) | 동일 후보 비교·서로 다른 선택 기준·표시 순서·불리한 모델 선택 보존·실패 기록 검사 |
| [test_traffic_situation_adapter.py](../tests/test_traffic_situation_adapter.py) | 경로·상태 결합과 이동·예약·잘못된 입력의 보류 검사 |
| [test_traffic_joint_action_scenario.py](../tests/test_traffic_joint_action_scenario.py) | 기존 가상 로봇 연결, AI 경로 생성, 모델 실패·중단 시 주행 차단 검사 |
| [test_order_scenario_runner.py](../tests/test_order_scenario_runner.py) | 주문 등록 일정, AI 요청 형식, 실제 모드·기존 작업의 등록 차단 검사 |
| [test_order_stream_scenario.py](../tests/test_order_stream_scenario.py) | 자동 주문 상한·기존 배차 연결·공급 중지·비상정지·실제 모드 차단 검사 |

### 문서

- 이 문서: 기존/추가/수정 범위와 흐름을 파악하는 시작점.
- [traffic-joint-action-test.md](traffic-joint-action-test.md): 교통조정 입력·실행 시험 상세.
- [order-scenario-test.md](order-scenario-test.md): 일반 주문 다중 등록 절차.
- [continuous-order-traffic-design.md](continuous-order-traffic-design.md): 별도로 작성한 연속 주문 처리 설계안. 문서 작성만 했으며 해당 설계 전체를 운영 코드에 적용한 것은 아니다.

## 5. 흐름이 어떻게 달라졌는가

### A. 일반 주문 처리: 새 출발 요청의 교통조정 연결

```text
화면 또는 주문 시험 실행기
  → POST /api/orders
  → PENDING 저장
  → 약 0.1초마다 배차 가능 여부 확인
  → 가용 로봇에 할당, IN_PROGRESS
  → 이번 구간의 목적지 결정
  → dispatch_order_route
      Algorithm: 기존 알고리즘 주행 경로
      AI: preview_llm_route(selector=kev) → 검증한 경로
      시뮬·AI + 일반 주문 교통조정 설정 켬:
        order_traffic_service → 기존 경로·동적 우회 검사 → 복수 후보면 KEV 선택
        → 최신 점유·예약 재검사 → 새 요청의 예약만 추가
  → 기존 TrafficManager와 주행기
  → 픽업·패킹·완료·복귀 또는 다음 주문
```

새 다중 주문 실행기는 이 흐름에 입력을 반복해서 넣는다. 배차 주기나 주행 코드를 대신하지 않는다.
**일반 주문은 새 정지 요청용 `order_traffic_service`에 연결했다.** 기존 `TrafficSituation`의 전원 정지 계약을 억지로 재사용하지 않는다.
다른 로봇의 주행·예약은 유지하고, 새 요청의 기존 경로·동적 우회 중 예약 가능한 것을 선택한다.
`traffic_route_provider`가 기존 A*/AI 경로 생성기에 회피 노드·물리 엣지를 전달한다.
선택 후 최신 점유·예약으로 다시 검사해 새 로봇의 예약만 추가한다. 이동 중 로봇의 경로 변경·공동 우선순위 변경은 이후다.
일반 주문 전체 흐름을 쓰되 시뮬·AI와 `FMS_ORDER_TRAFFIC_ENABLED=1`에서만 적용한다.
설정·검사·화면 안내: [일반 주문 연결](order-traffic-integration.md).

### B. 기존 지도 이동 시험: AI 경로 생성 연결 추가

```text
변경 전: 지도 시험 선택 → 시작 위치 확인/복원 → 기존 이동 명령

변경 후:
지도 시험 선택 → 시작 위치 확인/복원
  ├─ Algorithm: 기존 이동 명령
  └─ AI: 각 로봇의 KEV 경로 생성 → 경로·상태 검사 → required_path로 이동
```

이 8개 시험은 계속 기존 교통제어를 사용한다. 새 JointAction 선택 시험은 아니다.

### C. 새 FMS 입력 연결 시험: 경로부터 교통조정까지 연결

```text
kev_joint_fms_input 선택
  → 가상 시작 위치 복원: R1=9, R2=5, R3=0
  → 같은 잠금에서 전체 로봇 상태·예약·보호 자원 복사
  → R1 목적지 11, R2 목적지 8에 대한 경로 생성
      경로 생성 선택 A*(기본): plan_route
      경로 생성 선택 KEV: preview_llm_route(selector=kev, model=kev-08b)
      관제 operation_mode와 독립적이며 교통조정 선택기는 KEV
  → 경로 DTO + 전체 FMS 상태 + 지도·판단 설정
  → build_traffic_situation_input
  → TrafficSituation 검증
  → 코드로 조정안 생성·기존 TrafficManager로 사전 검사
  → 여러 후보면 KEV 선택, 후보 하나면 호출 생략
  → 상태·예약 재확인
  → 기존 가상 주행기로 선택 조정안의 steps 순차 실행
  → 도착 확인·결과 로그
```

경로 DTO는 `order.robot_id`, `current_node`, `node`, `result.path` 형태다.
FMS 상태는 전체 로봇의 실제 점유·주행·연결 상태와 자원 정보다.
설정은 대기 허용 노드, 차단 노드·엣지, 요청 순서·대기 시간, 선택적인 우회·양보 경로다.
현재 요청 순서·대기 시간은 시나리오 순서·0초라는 실험값이다.
요청 없는 R3도 정적 점유로 포함한다. 어댑터 자체는 KEV를 호출하지 않는다.

### D. 고정 경로 시험: 경로 생성과 분리해 조정만 검사

경로를 JSON으로 공급해 우회·양보·해결 불가 조건을 고정한다.
현재 동적으로 우회·양보 경로를 다시 생성하는 시험은 아니다.
여러 실행 가능한 조정안이 있을 때 KEV로 하나를 선택한다.

## 6. 모드별로 어떤 시험을 실행해야 하는가

| 시험 | 시뮬·알고리즘 | 시뮬·AI | 새 JointAction 선택 |
| --- | --- | --- | --- |
| 기존 일반 지도 8개 | 기존 알고리즘 이동 | KEV 경로 생성 후 이동 | 사용 안 함 |
| `kev_joint_fms_input` | A*(기본)/KEV 별도 선택 | A*(기본)/KEV 별도 선택 | 사용. 여러 후보면 KEV |
| `kev_joint_wait_or_reroute` | 공급 경로 사용 | 공급 경로 사용 | A2와 B_REROUTE 중 KEV 선택 |
| `kev_joint_yield_or_reroute` | 공급 경로 사용 | 공급 경로 사용 | B_YIELD_robot2와 B_REROUTE 중 KEV 선택 |
| `kev_joint_proceed_wait` | 공급 경로 사용 | 공급 경로 사용 | 사용. 여러 후보면 KEV |
| `kev_joint_reroute` | 공급 우회 경로 사용 | 공급 우회 경로 사용 | 사용. 후보 하나면 생략 |
| `kev_joint_yield` | 공급 양보·복귀 경로 사용 | 공급 양보·복귀 경로 사용 | 사용. 후보 하나면 생략 |
| `kev_joint_hold` | 공급 경로로 해결 불가 확인 | 공급 경로로 해결 불가 확인 | 단일 HOLD_ALL이면 생략 |
| 다중 일반 주문 | 기존 알고리즘 경로 | 기본 기존 AI 경로. 설정으로 A* 분리 시험 가능 | 일반 주문 설정을 켜면 새 출발 경로 조정 |
| `continuous_ai_orders` | 시작 전 거부 | 일반 주문으로 공급 | 같은 일반 주문 설정에 따라 적용 |

고정 경로 시험은 “시뮬·알고리즘”으로 실행해도 해당 시험의 조정안 선택 단계에서 KEV를 사용할 수 있다.
FMS 입력 연결 시험은 관제 모드와 경로 생성 선택을 분리한다. 일반 지도 시험과 주문의 기존 모드 정책은 유지한다.
새 KEV-MAPF 7개와 일반 주문 실행기는 가상 시험 전용이다.

### 과거 고정 경로 시험의 409 제한: 현재 해제

```text
고정 경로 시나리오는 시뮬·알고리즘에서 실행하세요.
AI 경로는 FMS 입력 연결 시나리오에서 검증합니다.
```

이 응답은 과거 고정 경로 시험을 KEV 경로 생성 시험으로 오해하지 않도록 넣었던 제한이다.
경로 생성과 교통조정 선택을 분리한 뒤 제거했으며, 현재 고정 경로 4개도 시뮬·AI에서 실행한다.
화면에 ‘경로: 시험용 고정 경로’를 표시한다. 일반 주문과 일반 지도 시험의 경로 생성 정책은 유지한다.

- 교통조정만 먼저 확인: **시뮬레이션 → 입력 연결 · A*/KEV 경로로 2대 합류 → 경로 생성: A***.
- KEV 경로 생성까지 확인: 같은 시험에서 **경로 생성: KEV** 선택. 두 경우 모두 교통조정은 KEV이며 관제 알고리즘/AI 모드와 독립적이다.
- 지정한 우회·양보·전원 대기 조건만 확인: 기존 작업 종료 후 **시뮬·AI → 해당 고정 경로 항목**. 시뮬·알고리즘도 가능하다.
- 일반 주문 여러 건 확인: 드롭다운 대신 아래 주문 실행기를 사용한다.

## 7. 실행 진입점과 로그

관제 주소: <http://100.81.82.30:5173/>. 현재 원격 Ubuntu의 주소이며 다른 PC에서는 환경에 맞춘다.

```bash
# 지도 시험: 관제 모드를 먼저 맞춘 뒤 실행. 화면 테스트 실행과 같은 API 사용.
python3 /home/woozoo/dev/Logistics_AI/tests/traffic_scenarios.py kev_joint_fms_input

# 일반 주문 입력 미리보기. 서버에 등록하지 않는다.
python3 /home/woozoo/dev/Logistics_AI/tests/order_scenarios.py bulk_six --operation ai

# 시뮬·AI에서 일반 주문 6건 등록·조회
python3 /home/woozoo/dev/Logistics_AI/tests/order_scenarios.py bulk_six --operation ai --run
```

`bulk_six`는 6건 연속 등록, `continuous_six`는 0초 3건·10초 2건·20초 1건 등록이다.
이 일정은 시험용 주문 유입 시각이며 FMS 배차 주기와 별개다.
실행기가 모드를 자동 전환하거나 기존 주문을 취소·초기화하지 않는다.

| 기록 | 위치 | 주의 |
| --- | --- | --- |
| 관제 교통조정 입력·후보·결과 | `logs/traffic-joint-actions/ui-last-*.json` | 다음 실행이 덮어씀 |
| 실제 KEV 경로+조정 시험 결과 | `logs/traffic-joint-actions/ai-route-joint-live-20261019.json` | 경로 생성과 조정안 선택 호출 수 별도 기록 |
| 다중 주문 시험 결과 | `logs/order-scenarios/TEST-*.json` | 생성한 주문 ID와 최종 조회 상태 보관 |

로그는 Git에 넣는 구현 파일과 별개다. 서버를 재시작하면 메모리의 주문·시나리오 상태가 초기화될 수 있다.

## 8. 검증한 범위와 남은 범위

작성 시점에 관련 자동 검사 **80개**가 통과했다. 입력 검증·호출 연결·오류 시 차단·기존 주문과 예약 동작의 코드 검사이며,
필요한 곳에는 모델 응답 대역을 사용한다. 실제 KEV가 80개 교통 상황을 올바르게 해결했다는 뜻은 아니다.

실제 서버 시험 1건에서는 KEV로 R1=[9,7,11], R2=[5,7,8] 경로를 생성했다.
경로 생성 추론 4회 + 조정안 선택 1회로 서버 요청도 총 5회 증가했다.
A2 선택 후 R2→R1 순서로 약 11초(11.06초)에 완료했다. 모델 추론 시간만의 측정이 아니라 가상 주행을 포함한 시간이다.
자동 검사의 상세 명령·실제 기록은 교통조정 검증 가이드에서 확인한다.

초기 작성 시점에는 정해진 일반 주문 6건의 실제 서버 실행을 하지 않았고, CLI의 등록 일정 검증·미리보기까지 준비했다.
이후 1단계로 관제의 연속 주문 공급·중지 기능을 추가했다. 검증 수치는 실행 시점별 기록으로 구분한다.
추가 후 자동 검사 90개와 프런트엔드 타입 검사·빌드가 통과했다.
실제 연속 공급 시험에서는 5건 중 1건 완료·4건 경로 생성 실패 후 복귀를 확인했다.
기존 KEV의 N5→N3 경로 탐색 오류이며, 공급기는 오류 시 중지했다. 별도 UI 시험에서
공급 중지 후 초기 3건 유지·6초간 추가 등록 없음도 확인했다. 자세한 기록은 일반 주문 시험 문서에 있다.

이후 2026-10-21까지 확인한 내용:

- 일반 주문의 새 이동 요청을 실제 위치·경로·예약으로 조정하는 `order_traffic_service` 연결. 정지 상태용 어댑터와 별도 입력 형식을 사용하며 다른 로봇의 주행 계획은 유지한다.
- 기존 생성기로 동적 우회 경로를 요청하고, 검사한 원래 경로·우회 경로 중 선택하는 연결.
- 앞 로봇이 픽업 노드를 떠나면 다음 로봇이 이동하는 동작과 홈 복귀 전 새 주문 배차.
- 같은 18건·57개 주문을 두 선택 방식으로 각각 처리한 [연속 비교](experiments/continuous-order-validation-20261021.md). 총 36건 완료이며 KEV의 우수성은 확인하지 못했다.
- [단계별 주문 취소 후 재개](experiments/order-cancellation-followup-20261021.md). 사용자가 취소한 주문의 기존 알고리즘 복귀와 뒤 주문 처리를 확인했다. KEV 다중 양보 자동 적용과는 다른 기능이다.

아직 남은 작업:

- 관련 로봇 묶음을 자동으로 찾아 정지 상태 다중 양보 탐색에 연결하는 기능. 현재 그 확장 탐색은 별도 시나리오다.
- 주행 중 기존 로봇 계획까지 바꾸는 공동 조정과 사건 기반 재판단. 일반 주문의 새 요청 한 건 조정과 구분한다.
- 양보 위치·양보 경로의 동적 생성과 실제 대기 시간을 반영한 장기 대기 보호.
- 다양한 부하·반복 실행의 처리량·공정성·모델 판단 품질 비교.
- 연결 끊김·장시간 막힌 복귀·실기체 정지와 물품 처리 검증.

이번 작성은 구현 현황 문서화이며 코드 병합·커밋·푸시는 하지 않았다. jhlee의 경로 DTO 형식은 참고하지만 해당 브랜치의 수집기를 병합한 상태는 아니다.
