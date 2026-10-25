FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# いちばん新しい版の neverland_trader_vN.py を自動で選んで起動する（版を上げるときは .py を上げるだけでよい）
CMD ["sh", "-c", "exec python $(ls neverland_trader_v*.py | sort -V | tail -n 1)"]
