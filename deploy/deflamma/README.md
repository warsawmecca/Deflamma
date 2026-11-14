# Deflamma · deflamma.duckdns.org 공개 설정

대상: Ubuntu 24.04, nginx, /home/ubuntu/himawari_rise_monitor,
himawari.service, Python 127.0.0.1:8765. 설정 파일을 준비한 상태이며 실제 서버에는 적용하지 않았습니다.
서비스 이름이 다르면 `setup.sh`의 himawari를 실제 이름으로 바꾸세요.

## 1. DNS와 포트

DuckDNS의 deflamma 레코드 IPv4를 **Oracle 인스턴스 공인 IPv4**로 설정합니다.
집/PC IP를 넣지 마세요. 사용하지 않는 IPv6 레코드는 비워 두세요.
DuckDNS 토큰은 업로드하거나 공유할 필요가 없습니다.
공인 IP가 바뀌면 DuckDNS도 갱신해야 합니다.

Oracle VCN 보안 목록/NSG에서 출발지 0.0.0.0/0에 대해 TCP 80,443을 허용합니다.
8765 포트는 열지 않습니다. 서버 방화벽도 80,443을 허용해야 합니다.
우선 아래 명령으로 현재 상태를 확인합니다. 방화벽을 초기화하지 마세요.

```bash
getent ahostsv4 deflamma.duckdns.org
sudo ufw status
sudo iptables -S INPUT
```

UFW가 active이면 `sudo ufw allow 80/tcp`와 `sudo ufw allow 443/tcp`를 실행합니다.
UFW가 inactive이고 OCI 기본 iptables의 마지막 REJECT 규칙이 차단한다면:

```bash
sudo iptables -I INPUT 1 -p tcp -m multiport --dports 80,443 -m conntrack --ctstate NEW -j ACCEPT
```

`netfilter-persistent`가 설치되어 있으면 `sudo netfilter-persistent save`로 보존합니다.
없으면 `sudo apt-get install iptables-persistent`를 설치하고 현재 IPv4 규칙 저장에 Yes를 선택합니다.
UFW가 inactive라는 이유만으로 새로 enable하지 마세요.

## 2. 수정 파일 업로드

Windows PowerShell에서 키 경로, ZIP 경로, 서버 IP를 실제 값으로 바꿉니다.

```powershell
scp -i "C:\경로\ssh-key.key" "C:\경로\himawari_rise_monitor.zip" ubuntu@서버공인IP:/home/ubuntu/himawari_rise_monitor.zip
ssh -i "C:\경로\ssh-key.key" ubuntu@서버공인IP
```

서버 SSH에서 실행합니다. 기존 코드 폴더를 백업한 후 덮어씁니다.
관측 출력 /home/ubuntu/himawari-data와 기존 .venv는 ZIP에 포함되어 있지 않습니다.

```bash
cd /home/ubuntu
cp -a himawari_rise_monitor "himawari_rise_monitor.backup.$(date +%Y%m%d%H%M%S)"
sudo systemctl stop himawari
unzip -o himawari_rise_monitor.zip -d /home/ubuntu
sudo systemctl start himawari
cd /home/ubuntu/himawari_rise_monitor
sudo bash deploy/deflamma/setup.sh http
```

외부 브라우저로 http://deflamma.duckdns.org 에서 `HTTPS setup in progress.`가 보여야 합니다.
안 보이면 다음 단계로 넘어가기 전에 DNS/방화벽을 확인하세요.

## 3. 인증서와 HTTPS

```bash
sudo certbot certonly --webroot -w /var/www/letsencrypt --cert-name deflamma.duckdns.org -d deflamma.duckdns.org
sudo bash deploy/deflamma/setup.sh https
sudo certbot renew --dry-run
```

Certbot의 이메일 및 약관 질문에 직접 답합니다. 마지막 스크립트가 관리자 admin의
비밀번호를 두 번 묻습니다. 이미 비밀번호 파일이 있으면 덮어쓰지 않습니다.
인증서 발급 실패 시 http 설정을 유지하고 원인을 해결한 뒤 재시도합니다.
Nginx에 같은 도메인의 별도 설정이 있으면 중복 server_name을 제거해야 합니다.

## 사용과 확인

- 공개 지도: https://deflamma.duckdns.org/
- 관리자 로그인: https://deflamma.duckdns.org/admin (아이디 admin)
- 공개 조회: 점수/온도/변화량/저장된 관심 지점과 이력/완료된 과거 조회 결과.
- 로그인 없이 허용: 관심 지점 추가, 과거 시점 새 수집.
- 관리자 작업: 관심 지점 이름 변경·추적 해제.
- 관심 지점과 과거 검색은 각각 서버 전체 32건까지 보관하며 초과하면 가장 오래 등록한 항목부터 영구 삭제합니다.
- 관심 지점 삭제 시 해당 시계열도 삭제합니다. 과거 검색 삭제 시 원자료 캐시·지도·CSV·ZIP도 함께 삭제합니다.
- 실행/대기 중 과거 검색은 삭제하지 않습니다. 같은 완료 결과를 재조회해도 보관 순서는 바뀌지 않습니다.
- 지점별 시계열은 최근 24시간, 최대 576개만 보관합니다. 실시간 수집 실패 중에도 정리합니다.
- 관심 지점 목록은 모든 방문자가 보는 공유 목록입니다. 개인별 계정/목록 기능은 아닙니다.
- 공개 모드에서 직접 Python 포트에 연결할 수 없도록 127.0.0.1 바인딩을 유지합니다.
- 로그인 정보는 브라우저가 저장하며 다른 관리 작업에서 다시 물으면 같은 정보를 입력합니다.
- 인증서는 certbot.timer로 갱신하고 deploy hook에서 nginx를 reload합니다.

```bash
sudo systemctl status himawari nginx --no-pager
sudo journalctl -u himawari -n 50 --no-pager
curl -I https://deflamma.duckdns.org/
curl -i https://deflamma.duckdns.org/api/watches
curl -i -X POST -H 'Content-Type: application/json' -d '{}' https://deflamma.duckdns.org/api/watches
```

마지막 요청은 로그인 없이 API에 전달되지만 필수 입력이 없으므로 400이 정상입니다. 비밀번호 변경:
`sudo htpasswd /etc/nginx/himawari.htpasswd admin`.
공개 모드는 systemd drop-in으로만 켜며 SSH 터널의 기존 localhost API도 계속 동작합니다.

롤백은 백업의 maps.py를 복원하고 systemd drop-in public-origin.conf와
nginx sites-enabled/deflamma 링크를 제거한 뒤 daemon-reload 및 두 서비스를 재시작합니다.
기존 Nginx 사이트를 교체한 경우 저장된 deflamma.backup 파일도 복원하세요.

참고: https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html
https://eff-certbot.readthedocs.io/en/stable/using.html#webroot

기존 서버 통합 업데이트: UPDATE_DEFLAMMA.md. 저장 용량 산정: STORAGE.md.
