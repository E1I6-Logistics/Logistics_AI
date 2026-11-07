# dev 통합 검토 기록

정리일: 2026-11-07

## 정리 범위

최근 사용 브랜치의 실행 코드를 기준으로 프로젝트 README 형식을 통일하고, `Logistics_AI`의 네 서브모듈을 검토용 `dev` 커밋에 맞춘다. `Logistics_FMS`, `Logistics_WAF_RASPI` 등 다른 저장소는 이번 반영 대상이 아니다.

- 실행 코드·테스트 코드·YAML·지도·모델·설치 스크립트는 수정하지 않는다.
- 각 저장소의 새 커밋에는 README와 참고 문서만 포함한다.
- 부모 저장소는 문서 외에 `.gitmodules`의 추적 브랜치와 네 gitlink를 갱신한다.
- 원격 `main`과 기존 기능 브랜치는 유지한다. 이번 반영 대상은 다섯 저장소의 `dev`뿐이다.
- 기존 루트 README는 `docs/README_REFERENCE.md`에 참고본으로 보존한다. WAF는 기존 세부 문서를 유지하고 루트 README를 추가한다.

## 기준 브랜치와 고정 커밋

관제 코드의 기준은 `Logistics_AI/feat/fms-continuous-run`의 [`6d5bf76`](https://github.com/E1I6-Logistics/Logistics_AI/commit/6d5bf7693479bb24bfe3595c5f71378c5053abc2)이다. 기존 원격 `dev`의 이력을 포함한 브랜치이므로 이력을 덮어쓰지 않고 `dev`에 반영한다.

| 저장소 | 실행 코드 기준 브랜치 | 실행 코드 기준 커밋 | README 정리 후 고정한 dev 커밋 |
|---|---|---|---|
| [Logistics_WAF](https://github.com/E1I6-Logistics/Logistics_WAF/tree/dev) | `sso_dev` | [`1a46aff`](https://github.com/E1I6-Logistics/Logistics_WAF/commit/1a46aff2bb93ccfe3353cb90b2a835a5b1d3dc24) | [`8b85c2e`](https://github.com/E1I6-Logistics/Logistics_WAF/commit/8b85c2e5bbb0a09690114426f49ed121daadab41) |
| [Logistics_AMR](https://github.com/E1I6-Logistics/Logistics_AMR/tree/dev) | `dev` | [`bd0917d`](https://github.com/E1I6-Logistics/Logistics_AMR/commit/bd0917d67b3de9740619a4f29cfa214f229f3ae7) | [`d2372e8`](https://github.com/E1I6-Logistics/Logistics_AMR/commit/d2372e851dcf74526c95aee272fca7c38db3f86a) |
| [Logistics_OMX](https://github.com/E1I6-Logistics/Logistics_OMX/tree/dev) | `dev` | [`03438da`](https://github.com/E1I6-Logistics/Logistics_OMX/commit/03438da543e16a3d218e9734372ec05232a23fa0) | [`d5187b8`](https://github.com/E1I6-Logistics/Logistics_OMX/commit/d5187b8ae0deaf81adbe4938d8fa53801b5d5696) |
| [shellscript](https://github.com/E1I6-Logistics/shellscript/tree/dev) | `dev` | [`020ed11`](https://github.com/E1I6-Logistics/shellscript/commit/020ed11245d9fd93ad2511222e0de17c57161bdf) | [`394c258`](https://github.com/E1I6-Logistics/shellscript/commit/394c25881231814eeea0ddb028635e93e39290cd) |

WAF는 `sso_dev`의 실행 코드를 기준으로 `dev`를 새로 만든다. AMR은 이전 AX 폴더의 고정 버전이 아닌 현재 원격 `dev`를 사용한다. OMX와 shellscript도 원격 `dev`의 실행 코드를 유지한다.

## 서브모듈 사용 원칙

`.gitmodules`의 `branch = dev`는 이후 `--remote` 업데이트 시 참조할 브랜치를 지정한다. 실제 프로젝트 조합은 부모 저장소에 기록된 **커밋 ID**로 고정한다.

```bash
git switch dev
git pull --ff-only origin dev
git submodule update --init --recursive
git submodule status
```

위 명령은 부모 `dev`가 기록한 조합을 받는다. `git submodule update --remote`는 원격에서 새로 바뀐 버전까지 가져오므로 최종 검토 과정에서는 구분해 사용한다. 서브모듈이 detached HEAD로 표시되는 것은 고정 커밋을 사용하기 위한 정상 상태다.

로컬 브랜치가 원격과 갈라졌거나 다른 worktree에서 사용 중이면 강제 초기화하지 않는다. 이전 로컬 브랜치를 보존하고 원격 `dev`의 고정 커밋을 별도로 사용한다. 로컬 모델·보정·`.env`·build/install 자료는 Git 참조 갱신 대상이 아니다.

## 문서 점검 기준

1. 최근 사용 브랜치와 원격 커밋을 확인한다.
2. 기준 커밋 대비 변경 파일이 문서·허용한 Git 참조로만 구성됐는지 확인한다.
3. 실행 파일·설정 파일의 Git blob이 기준 커밋과 같은지 확인한다.
4. 다섯 README의 목차와 장 구성을 통일한다.
5. 문서 링크·앵커·이미지 경로를 확인한다.
6. Markdown 코드 블록과 한글 문자를 확인한다.
7. 실행 명령·카메라·모델 매핑·장비 역할을 현재 소스와 대조한다.
8. 설치·조회·실제 동작 명령과 기존 현장 설정의 적용 범위를 구분한다.
9. 부모의 gitlink와 자식 `dev` 커밋, 네 추적 브랜치를 대조한다.
10. 반영 후 `dev`와 실제 AX 체크아웃을 확인하고 원격 `main`이 유지됐는지 확인한다.

이는 문서와 Git 조합의 점검이다. 로봇 이동·서비스 재시작·설치·빌드를 다시 실행한 실물 시험 결과는 포함하지 않는다.

## 최종 검토 이후

[프로젝트 README](../README.md)와 각 서브모듈의 README를 검토하고 이미지·시연 자료를 보완한다. `main` 반영은 사용자의 최종 검토가 끝난 뒤 별도 요청에 따라 진행한다. 이번 정리에서는 `main`에 push하거나 기본 브랜치를 변경하지 않는다.
