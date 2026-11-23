# Third-party notices

| 항목 | 출처 | 라이선스 | 사용 방식 |
|---|---|---|---|
| `vendor/nemoclaw-schemas/*.json` | NemoClaw (`ai-agent-orchestrator/NemoClaw` 포크, 상류 `NVIDIA/NemoClaw`) commit `ccf8e4e` | Apache-2.0 | 원본 그대로 복사. 생성된 preset 검증 테스트에만 사용 |
| `orchestra/filters/builtin/code_guard.py` 의 규칙 아이디어 | Claude `security-guidance` 플러그인 (`claude-10-plugin-for-multi-agent/plugins/security-guidance`, Apache-2.0) `hooks/patterns.py` | Apache-2.0 | 위험 API 목록의 **아이디어만 참고**, 규칙·정규식은 런타임 차단용으로 새로 작성(코드·문구 복사 없음) |
| `orchestra/security/scanner.py` 의 판정 모델·`from_audit_json` | `security-audit-skill` `skills/security-audit/report-schema.json` | MIT (Cloudflare, Inc.) | 스키마 **형식만** 참고(필드 매핑). 코드·검증기 복사 없음 |
| `orchestra/security/garak_bridge.py` | garak (`ai-agent-orchestrator/garak`, 상류 NVIDIA/garak) 의 function generator 규약 및 리포트(JSONL) 형식 | Apache-2.0 | garak 을 import 하지 않고 CLI 규약·리포트 필드만 사용 |
| `AI-agents-for-cybersecurity` | 학습 자료 | — | 코드 미사용. 방어용 아이디어(KEV 우선순위 등)만 향후 참고. 포트스캔·공격 자동화 계열은 채택하지 않음 |

새 코드를 가져올 때는 이 표에 출처·라이선스·사용 방식을 추가한다.
