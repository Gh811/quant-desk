#!/bin/zsh
cd -- "${0:A:h}" || exit 1

if [[ -t 0 && ( -z "$BINANCE_DEMO_API_KEY" || -z "$BINANCE_DEMO_API_SECRET" ) ]]; then
    service='Quant Desk Binance Demo'
    if [[ "$1" != "--reset-key" ]]; then
        BINANCE_DEMO_API_KEY=$(security find-generic-password -s "$service" -a api-key -w 2>/dev/null)
        BINANCE_DEMO_API_SECRET=$(security find-generic-password -s "$service" -a api-secret -w 2>/dev/null)
    else
        shift
    fi
    if [[ -z "$BINANCE_DEMO_API_KEY" || -z "$BINANCE_DEMO_API_SECRET" ]]; then
        read -rs 'BINANCE_DEMO_API_KEY?币安模拟盘 API Key（回车跳过）: '
        echo
        if [[ -n "$BINANCE_DEMO_API_KEY" ]]; then
            read -rs 'BINANCE_DEMO_API_SECRET?币安模拟盘 API Secret: '
            echo
            if [[ -n "$BINANCE_DEMO_API_SECRET" ]]; then
                if ! security add-generic-password -U -s "$service" -a api-key -w "$BINANCE_DEMO_API_KEY" ||
                   ! security add-generic-password -U -s "$service" -a api-secret -w "$BINANCE_DEMO_API_SECRET"; then
                    echo '钥匙串保存失败，本次仍可使用输入的凭据。'
                fi
            fi
        fi
    fi
    export BINANCE_DEMO_API_KEY BINANCE_DEMO_API_SECRET
fi
exec python3 app.py "$@"
