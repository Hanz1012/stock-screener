"""
三合一選股評分機器人 (價值 / 成長 / 股息)
=========================================
每天執行一次，對台股 + 美股候選清單抓取基本面資料，
分別計算「價值分數」「成長分數」「股息分數」，
並依據分數組合建議適合的持有期間 (短期 / 中期 / 長期)。

輸出: docs/data.json  (給網頁儀表板讀取)
"""

import json
import time
import datetime
import statistics
import pandas as pd
import yfinance as yf

# ----------------------------------------------------------------------
# 1. 股票候選清單 (Universe)
# ----------------------------------------------------------------------

# 台股：元大台灣50 (0050) 成分股，可自行增減
TW_TICKERS = [
    "2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW", "2412.TW",
    "2881.TW", "2882.TW", "2891.TW", "1301.TW", "1303.TW", "1216.TW",
    "2886.TW", "2884.TW", "3711.TW", "2379.TW", "2357.TW", "2395.TW",
    "3034.TW", "2892.TW", "5880.TW", "2885.TW", "2880.TW", "1101.TW",
    "2207.TW", "3045.TW", "4938.TW", "2609.TW", "2603.TW", "6669.TW",
    "3008.TW", "2327.TW", "2887.TW", "5871.TW", "2883.TW", "1326.TW",
    "9910.TW", "2890.TW", "2801.TW", "6505.TW", "3037.TW", "2377.TW",
    "8046.TW", "2345.TW", "1590.TW", "2059.TW", "4904.TW", "2912.TW",
    "3231.TW", "6415.TW",
]

def get_us_tickers(limit=100):
    """從 S&P500 成分股清單抓取美股候選 (取市值/知名度較高的前 N 檔可自行調整)"""
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
    df = pd.read_csv(url)
    return df["Symbol"].tolist()[:limit]

# ----------------------------------------------------------------------
# 2. 抓取單一股票的基本面資料
# ----------------------------------------------------------------------

def fetch_fundamentals(ticker):
    try:
        info = yf.Ticker(ticker).info
        return {
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "pe": info.get("trailingPE"),
            "pb": info.get("priceToBook"),
            "roe": info.get("returnOnEquity"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
        }
    except Exception as e:
        print(f"  [跳過] {ticker}: {e}")
        return None

# ----------------------------------------------------------------------
# 3. 評分邏輯
# ----------------------------------------------------------------------

def percentile_rank(value, all_values):
    """把數值轉成 0~100 的相對排名分數 (數值愈高分數愈高)"""
    valid = [v for v in all_values if v is not None]
    if not valid or value is None:
        return 50  # 資料缺失給中間值，避免整體被拖垮
    rank = sum(1 for v in valid if v <= value) / len(valid)
    return round(rank * 100, 1)

def score_stocks(records):
    pe_list = [r["pe"] for r in records]
    pb_list = [r["pb"] for r in records]
    roe_list = [r["roe"] for r in records]
    rev_g_list = [r["revenue_growth"] for r in records]
    earn_g_list = [r["earnings_growth"] for r in records]
    div_y_list = [r["dividend_yield"] for r in records]
    payout_list = [r["payout_ratio"] for r in records]
    beta_list = [r["beta"] for r in records]

    for r in records:
        # 價值分數：本益比/股價淨值比「愈低愈好」-> 用反向排名；ROE 愈高愈好
        pe_score = 100 - percentile_rank(r["pe"], pe_list) if r["pe"] and r["pe"] > 0 else 50
        pb_score = 100 - percentile_rank(r["pb"], pb_list) if r["pb"] and r["pb"] > 0 else 50
        roe_score = percentile_rank(r["roe"], roe_list)
        r["value_score"] = round((pe_score + pb_score + roe_score) / 3, 1)

        # 成長分數：營收成長率 + 獲利成長率
        rev_score = percentile_rank(r["revenue_growth"], rev_g_list)
        earn_score = percentile_rank(r["earnings_growth"], earn_g_list)
        r["growth_score"] = round((rev_score + earn_score) / 2, 1)

        # 股息分數：殖利率愈高愈好，配息率不能過高(>100%表示配得比賺得多，較不健康)
        div_score = percentile_rank(r["dividend_yield"], div_y_list)
        payout_penalty = 0
        if r["payout_ratio"] and r["payout_ratio"] > 1:
            payout_penalty = 20
        r["dividend_score"] = round(max(div_score - payout_penalty, 0), 1)

        r["composite_score"] = round(
            (r["value_score"] + r["growth_score"] + r["dividend_score"]) / 3, 1
        )

        # 建議持有期間
        r["horizon"] = suggest_horizon(r)

    return records

def suggest_horizon(r):
    beta = r["beta"] or 1.0
    scores = {"value": r["value_score"], "growth": r["growth_score"], "dividend": r["dividend_score"]}
    top = max(scores, key=scores.get)

    if top == "dividend" and beta < 1.1:
        return "長期 (適合存股領息)"
    if top == "growth" and beta >= 1.1:
        return "短中期 (波動較大，成長動能為主)"
    if top == "value":
        return "中長期 (等待價值回歸)"
    return "中期 (可觀察為主)"

# ----------------------------------------------------------------------
# 4. 主流程
# ----------------------------------------------------------------------

def run(top_n=15):
    all_tickers = TW_TICKERS + get_us_tickers(100)
    records = []
    print(f"開始抓取 {len(all_tickers)} 檔股票資料...")
    for i, t in enumerate(all_tickers):
        data = fetch_fundamentals(t)
        if data:
            records.append(data)
        if i % 20 == 0:
            time.sleep(1)  # 避免請求過於頻繁

    scored = score_stocks(records)
    scored.sort(key=lambda r: r["composite_score"], reverse=True)

    output = {
        "updated_at": datetime.datetime.now().isoformat(),
        "total_scanned": len(scored),
        "top_picks": scored[:top_n],
        "all_results": scored,
    }

    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完成！已輸出前 {top_n} 檔候選股至 docs/data.json")

if __name__ == "__main__":
    run()
