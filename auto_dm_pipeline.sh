#!/bin/bash
# 自动流水线: 等登录成功 -> 确认cookie有效 -> 跑私信抓取
cd "/Users/a1412/Desktop/火花/douyin_qr_login"
PY=/usr/local/bin/python3
fails=0
for i in $(seq 1 150); do
  resp=$(curl -s --max-time 4 http://localhost:8765/api/status 2>/dev/null)
  st=$(echo "$resp" | $PY -c "import json,sys
try: print(json.load(sys.stdin).get('state','?'))
except: print('?')" 2>/dev/null)
  echo "[$i] state=$st"
  if [ "$st" = "success" ]; then
    echo "=== LOGIN_SUCCESS ==="
    sleep 6
    # 确认 cookie 里有真正的会话字段
    cf=$(ls -t output/session_*.json 2>/dev/null | head -1)
    echo "cookie_file=$cf"
    if [ -n "$cf" ]; then
      ok=$($PY -c "
import json,sys
d=json.load(open('$cf'))
names={c['name'] for c in d.get('cookies',[])}
print('yes' if names & {'sessionid','sessionid_ss','sid_tt','uid_tt'} else 'no')
" 2>/dev/null)
      echo "cookie_valid=$ok"
    fi
    break
  fi
  if [ "$st" = "?" ]; then
    fails=$((fails+1))
    if [ $fails -ge 20 ]; then echo "=== SERVER_DOWN_TOO_LONG ==="; break; fi
  else
    fails=0
  fi
  sleep 8
done
echo "=== RUN SCRAPER ==="
$PY dm_scraper.py --dump --scroll 3 2>&1
echo "=== SCRAPER_DONE exit=$? ==="
