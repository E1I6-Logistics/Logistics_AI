#!/usr/bin/env bash
# FMS 서버를 실행하는 사용자 계정에서 1회 실행. 비밀번호는 저장하지 않는다.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="$HOME/.ssh/fms_omx_ed25519"
for tool in ssh ssh-keygen ssh-copy-id python3; do
    command -v "$tool" >/dev/null || { printf '[SSH] 필요한 명령이 없습니다: %s\n' "$tool" >&2; exit 1; }
done
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -f "$KEY" -N '' -C 'fms-omx-service'
fi
[ -f "$KEY.pub" ] || { printf '[SSH] 공개키가 없습니다: %s.pub\n' "$KEY" >&2; exit 1; }
TARGETS=(omx01@192.168.0.151 omx02@192.168.0.152 omx03@192.168.0.153 omx4@192.168.0.154)
for target in "${TARGETS[@]}"; do
    printf '\n[SSH] %s: 장비 호스트키를 확인하고 Pi 로그인 비밀번호를 입력하세요.\n' "$target"
    ssh-copy-id -i "$KEY.pub" -o ConnectTimeout=5 "$target"
    ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 \
        "$target" 'systemctl --user show-environment >/dev/null'
    printf '[SSH] 접속·사용자 서비스 환경 확인 완료: %s\n' "$target"
done
python3 - "$ROOT/.env" "$KEY" <<'ENV_PY'
import json,os,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1]);key=sys.argv[2]
targets=dict(zip(('omx1','omx2','omx3','omx4'),('omx01@192.168.0.151','omx02@192.168.0.152','omx03@192.168.0.153','omx4@192.168.0.154')))
values={'FMS_OMX_SSH_TARGETS':json.dumps(targets,separators=(',',':')),'FMS_OMX_SSH_IDENTITY':key}
lines=path.read_text().splitlines() if path.exists() else []
lines=[line for line in lines if line.split('=',1)[0] not in values]
lines.extend(k+'='+v for k,v in values.items())
mode=path.stat().st_mode & 0o777 if path.exists() else 0o600
fd,name=tempfile.mkstemp(prefix='.env-omx-',dir=path.parent)
try:
    with os.fdopen(fd,'w') as file:file.write('\n'.join(lines)+'\n')
    os.chmod(name,mode);os.replace(name,path)
finally:
    Path(name).unlink(missing_ok=True)
print('[SSH] FMS .env에 장비 주소·키 경로 저장 완료. 비밀번호는 저장하지 않았습니다.')
ENV_PY
printf '\n[SSH] FMS를 다시 시작하면 OMX 서비스 재시작 버튼을 사용할 수 있습니다.\n'
