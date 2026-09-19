#!/bin/bash
# デプロイの実体は scripts/trader.sh deploy に一本化している
# （git pull → pip install → alembic upgrade head → head確認）。
# 手順が2系統あると片方だけマイグレーションが抜ける事故が起きるため、ここでは委譲するだけにする。
exec "$(cd "$(dirname "$0")" && pwd)/trader.sh" deploy
