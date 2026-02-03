"""
R-Risk Manager OS Dashboard
리스크 관리 시스템: 포트폴리오 추적, TOR 모니터링, 성적표 관리
"""

# ============================================================================
# [1. IMPORTS & CONFIGURATION]
# ============================================================================
import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots
from datetime import datetime, timedelta

# 데이터베이스 경로 설정
DB_PATH = "risk_manager.db"

# ========== 리스크 파라미터 상수 ==========
BASE_1R_PCT = 0.01          # Green 국면 기준 1R (Equity의 1%)
MAX_POS_SIZE_PCT = 0.20     # 단일 종목 최대 투입 비중 (Equity의 20%)
                             # 근거: 손절폭이 극단적으로 좁을 때 발생하는
                             #       물리적 집중 리스크 차단 (Slippage Defense)

# ============================================================================
# [2. DATABASE FUNCTIONS]
# ============================================================================

def get_db_connection():
    return sqlite3.connect(DB_PATH, timeout=30)

def init_db():
    """데이터베이스 테이블 초기화"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 포트폴리오 테이블: 현재 보유 포지션 (+ initial_stop_loss 추가)
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                 (ticker TEXT PRIMARY KEY, entry_price REAL, stop_loss REAL, 
                  quantity INTEGER, sector TEXT, entry_date TEXT, breakdown_low REAL, initial_stop_loss REAL)''')
    
    # 매매 기록 테이블: 청산된 포지션의 성적표
    c.execute('''CREATE TABLE IF NOT EXISTS trade_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, entry_date TEXT, 
                  exit_date TEXT, entry_price REAL, exit_price REAL, r_multiple REAL)''')
    
    conn.commit()
    conn.close()
    
    # 스키마 업데이트 (기존 테이블에 컬럼 추가)
    update_db_schema()
    init_account_db()

def init_account_db():
    """계좌 메타데이터 테이블 초기화"""
    conn = get_db_connection()
    c = conn.cursor()
    # 계좌의 총 자산을 저장하는 테이블 (단일 로우만 사용)
    c.execute('''CREATE TABLE IF NOT EXISTS account_config
                 (id INTEGER PRIMARY KEY, total_equity REAL, last_updated TEXT)''')
    
    # 초기 데이터가 없을 경우 10,000달러로 세팅
    c.execute("SELECT COUNT(*) FROM account_config")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO account_config (id, total_equity, last_updated) VALUES (1, 10000.0, ?)",
                  (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
    conn.commit()
    conn.close()

def get_total_equity():
    """저장된 총 자산(Total Equity) 조회"""
    conn = get_db_connection()
    c = conn.cursor()
    # 테이블이 없을 경우 대비 (안전장치)
    try:
        c.execute("SELECT total_equity FROM account_config WHERE id = 1")
        result = c.fetchone()
        equity = result[0] if result else 10000.0
    except:
        equity = 10000.0
    conn.close()
    return equity

def update_total_equity(new_equity):
    """총 자산 강제 업데이트 (수동 수정용)"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE account_config SET total_equity = ?, last_updated = ? WHERE id = 1",
              (new_equity, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def adjust_equity_by_amount(amount):
    """금액만큼 자산 가감 (청산 수익 반영 또는 입출금)"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE account_config SET total_equity = total_equity + ?, last_updated = ? WHERE id = 1",
              (amount, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def update_db_schema():
    """데이터베이스 스키마 업데이트: 기존 테이블에 새 컬럼 추가"""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 1. 20SMA 이탈 시의 저가를 기록할 컬럼 추가
        c.execute("ALTER TABLE portfolio ADD COLUMN breakdown_low REAL")
    except sqlite3.OperationalError:
        pass

    try:
        # 4. Initial Stop Loss 컬럼 추가 (R 계산 고정 분모용)
        c.execute("ALTER TABLE portfolio ADD COLUMN initial_stop_loss REAL")
    except sqlite3.OperationalError:
        pass
    
    try:
        # 2. Trade ID 컬럼 추가 (Ticker_EntryDate 조합)
        c.execute("ALTER TABLE trade_history ADD COLUMN trade_id TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        # 3. Exit Quantity 컬럼 추가 (비중 계산용)
        c.execute("ALTER TABLE trade_history ADD COLUMN exit_qty INTEGER")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()
    
    # 데이터 마이그레이션 (기존 데이터에 Trade_ID 및 Exit_Qty 부여)
    migrate_trade_data()

def migrate_trade_data():
    """기존 매매 기록에 Trade_ID 및 Exit_Qty 일괄 부여"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. Trade_ID가 없는 레코드 조회
    c.execute("SELECT id, ticker, entry_date FROM trade_history WHERE trade_id IS NULL")
    rows = c.fetchall()
    
    for r in rows:
        row_id, ticker, entry_date = r
        # 혹시 Ticker에 '(P)'가 붙어있으면 제거 (과거 데이터 클렌징)
        clean_ticker = ticker.replace("(P)", "").strip()
        
        # Trade ID 생성: Ticker_EntryDate
        generated_id = f"{clean_ticker}_{entry_date}"
        
        # 업데이트
        c.execute("UPDATE trade_history SET trade_id = ?, exit_qty = 1 WHERE id = ?", 
                  (generated_id, row_id))
    
    # 2. Exit_Qty가 없는 레코드 (NULL) -> 1로 기본값 설정
    c.execute("UPDATE trade_history SET exit_qty = 1 WHERE exit_qty IS NULL")
    
    conn.commit()
    conn.close()

def get_current_price(ticker):
    """yfinance를 통해 현재가 조회"""
    try:
        data = yf.Ticker(ticker).history(period="1d")
        return data['Close'].iloc[-1] if not data.empty else None
    except:
        return None

def add_position(ticker, entry, stop, qty, sector):
    """
    새 포지션 추가 및 기존 포지션 병합(WAC 적용) 로직
    """
    conn = get_db_connection()
    try:
        c = conn.cursor()
        ticker = ticker.upper()
        
        # 1. 기존 포지션 존재 여부 확인
        c.execute("SELECT entry_price, quantity, initial_stop_loss FROM portfolio WHERE ticker = ?", (ticker,))
        existing_pos = c.fetchone()
        
        if existing_pos:
            old_price, old_qty, old_init_stop = existing_pos
            
            # 2. 가중평균단가(WAC) 및 합산 수량 계산
            total_qty = old_qty + qty
            # 공식: ((기존단가 * 기존수량) + (신규단가 * 신규수량)) / 총수량
            wac_price = ((old_price * old_qty) + (entry * qty)) / total_qty
            
            # 병합 시 Initial Stop은? 
            # 원칙적으로 신규 진입분의 리스크가 섞이므로 복잡하지만, 
            # 단순화를 위해 "가장 최근 진입 시점의 Stop"을 새로운 기준(Initial Stop)으로 갱신하거나,
            # 혹은 기존 Initial Stop을 유지할지 결정해야 합니다.
            # 여기서는 '물타기/불타기' 시 새로운 평단/수량에 맞춰 리스크 구조가 재편된다고 보고
            # stop(신규 입력값)을 새로운 initial_stop_loss로 설정하는 것이 합리적입니다 (유저 의도에 따라 조정 가능)
            # 하지만, "분모 불변" 원칙을 위해선 최초 진입 리스크를 유지해야 할 수도 있습니다.
            # *USER Context*: 불타기(Pyramiding)시 보통 평단이 올라가고 스탑도 올립니다. 
            # 따라서 병합 시에는 새로운 Stop을 Initial Stop으로 간주하겠습니다.
            
            c.execute("""UPDATE portfolio 
                         SET entry_price = ?, quantity = ?, stop_loss = ?, sector = ?, initial_stop_loss = ?
                         WHERE ticker = ?""",
                      (wac_price, total_qty, stop, sector, stop, ticker))
            st.toast(f"✅ {ticker}: {qty}주가 기존 포지션에 병합되었습니다. (신규 평단: ${wac_price:.2f})")
        
        else:
            # 4. 신규 포지션인 경우 (기존 INSERT 로직)
            # initial_stop_loss에도 stop 값을 저장
            c.execute("""INSERT INTO portfolio 
                         (ticker, entry_price, stop_loss, quantity, sector, entry_date, initial_stop_loss) 
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (ticker, entry, stop, qty, sector, 
                       datetime.now().strftime('%Y-%m-%d'), stop))
            st.toast(f"🚀 {ticker}: 신규 포지션 {qty}주가 추가되었습니다.")
            
        conn.commit()
    except Exception as e:
        st.error(f"데이터베이스 오류: {e}")
    finally:
        conn.close()

def get_portfolio():
    """현재 포트폴리오 조회"""
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM portfolio", conn)
    conn.close()
    return df

def delete_position(ticker):
    """포지션 삭제 (청산 전 단순 삭제용)"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM portfolio WHERE ticker=?", (ticker,))
    conn.commit()
    conn.close()

def close_position(ticker, exit_price, qty_to_close):
    """포지션 청산 (전체 또는 일부): Trade History 기록 및 Equity 반영"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 기존 포지션 데이터 조회 (Initial Stop 포함)
    c.execute("SELECT entry_price, stop_loss, quantity, entry_date, initial_stop_loss FROM portfolio WHERE ticker=?", (ticker,))
    row_data = c.fetchone()
    
    if row_data:
        entry_p, stop_p, current_qty, entry_date, init_stop = row_data
        if init_stop is None: init_stop = stop_p # Fallback
        
        # 수량 유효성 검사 (보유량보다 크면 전량 청산으로 간주)
        if qty_to_close > current_qty:
            qty_to_close = current_qty
            
        # R Unit (불변 분모) = |Entry - Initial Stop|
        r_unit = abs(entry_p - init_stop)
        
        # R Multiple 계산
        r_multiple = (exit_price - entry_p) / r_unit if r_unit != 0 else 0
        
        # Trade ID 생성 (Ticker_EntryDate)
        trade_id = f"{ticker}_{entry_date}"
        
        # 매매 기록 저장
        c.execute("""INSERT INTO trade_history 
                     (ticker, entry_date, exit_date, entry_price, exit_price, r_multiple, trade_id, exit_qty) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (ticker, entry_date, datetime.now().strftime('%Y-%m-%d'), 
                   entry_p, exit_price, r_multiple, trade_id, qty_to_close))
        
        # 실제 실현 손익(Realized P&L) 계산 및 자산(Equity)에 직접 반영 (Lock 방지)
        pnl_dollars = (exit_price - entry_p) * qty_to_close
        c.execute("UPDATE account_config SET total_equity = total_equity + ?, last_updated = ? WHERE id = 1",
                  (pnl_dollars, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        
        # 포트폴리오 업데이트: 잔여 수량 있으면 Update, 없으면 Delete
        remaining_qty = current_qty - qty_to_close
        if remaining_qty > 0:
            c.execute("UPDATE portfolio SET quantity = ? WHERE ticker = ?", (remaining_qty, ticker))
            st.toast(f"📉 {ticker}: {qty_to_close}주 청산 완료 (잔여: {remaining_qty}주)")
        else:
            c.execute("DELETE FROM portfolio WHERE ticker=?", (ticker,))
            st.toast(f"🏁 {ticker}: 포지션 완전히 청산되었습니다.")
    
    conn.commit()
    conn.close()

def get_trade_history():
    """매매 기록 조회"""
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM trade_history ORDER BY exit_date DESC", conn)
    conn.close()
    return df

def delete_selected_trades(trade_ids):
    """선택된 ID의 매매 기록만 삭제"""
    if not trade_ids:
        return
    
    conn = get_db_connection()
    c = conn.cursor()
    # SQL IN 구문을 사용하여 여러 ID를 한 번에 처리
    placeholders = ','.join(['?'] * len(trade_ids))
    query = f"DELETE FROM trade_history WHERE id IN ({placeholders})"
    c.execute(query, trade_ids)
    conn.commit()
    conn.close()


def update_stop_loss(ticker, new_stop_price):
    """스탑 로스 가격 업데이트"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE portfolio SET stop_loss = ? WHERE ticker = ?", (new_stop_price, ticker))
    conn.commit()
    conn.close()

def process_partial_exit(ticker, exit_qty, exit_px, entry_px, current_1r_unit):
    """분할 매도 처리 프로세스"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. 분모(Initial Risk) 확보
    # portfolio 테이블에서 initial_stop_loss 가져오기
    c.execute("SELECT initial_stop_loss, stop_loss, entry_date FROM portfolio WHERE ticker = ?", (ticker,))
    row = c.fetchone()
    
    if row:
        init_stop, current_stop, entry_date = row
        # init_stop이 NULL이면(구 데이터) 현재 stop_loss를 fallback으로 사용
        calc_stop = init_stop if init_stop is not None else current_stop
        
        # R Unit 계산 (불변 분모)
        r_unit_fixed = abs(entry_px - calc_stop)
        
        # R Multiple 계산
        if r_unit_fixed > 0:
            realized_r = (exit_px - entry_px) / r_unit_fixed
        else:
            realized_r = 0
            
        original_entry_date = entry_date
    else:
        # 포트폴리오에 없는 경우(예외), 기존 로직 Fallback
        realized_r = 0
        original_entry_date = datetime.now().strftime('%Y-%m-%d')
        r_unit_fixed = 0

    trade_id = f"{ticker}_{original_entry_date}"
    
    c.execute("""INSERT INTO trade_history (ticker, entry_date, exit_date, entry_price, exit_price, r_multiple, trade_id, exit_qty) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
              (f"{ticker}(P)", original_entry_date, datetime.now().strftime('%Y-%m-%d'),
               entry_px, exit_px, realized_r, trade_id, exit_qty))
    
    # 실제 실현 손익(Realized P&L in Dollars) 계산 및 자산 반영
    pnl_dollars = (exit_px - entry_px) * exit_qty
    c.execute("UPDATE account_config SET total_equity = total_equity + ?, last_updated = ? WHERE id = 1",
              (pnl_dollars, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    
    # 2. 포트폴리오 수량 차감
    c.execute("UPDATE portfolio SET quantity = quantity - ? WHERE ticker = ?", (exit_qty, ticker))
    
    # 3. 수량이 0 이하가 되면 포지션 삭제
    c.execute("SELECT quantity FROM portfolio WHERE ticker = ?", (ticker,))
    remaining_qty = c.fetchone()[0]
    if remaining_qty <= 0:
        c.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))
    
    conn.commit()
    conn.close()
    st.toast(f"{ticker} {exit_qty}주 분할 매도 완료 (Realized: {realized_r:.2f}R)")

def calculate_real_expectancy(df):
    """
    분할 청산을 반영하여 'Trade ID' 기준으로 실제 기댓값(Expectancy)을 산출하는 함수
    """
    if df.empty:
        return 0, 0, 0
        
    # 0. 필수 컬럼 확인 (오류 방지)
    if 'trade_id' not in df.columns or 'exit_qty' not in df.columns:
        # 컬럼이 없는 경우(마이그레이션 전 등) 기존 방식으로 계산
        return df['r_multiple'].mean(), (df['r_multiple'] > 0).mean() * 100, len(df)

    # 1. Trade_ID 별로 그룹화하여 데이터 집계
    # 필요한 것: 
    # - Total Realized Profit ($) = sum( (ExitPrice - EntryPrice) * ExitQty )
    # - Total Initial Risk ($) = (EntryPrice - InitialStop) * TotalQty
    #   문제는 'TotalQty'와 'InitialStop' 정보가 Trade History 테이블에 온전히 다 있지 않을수도 있음 (분할매도 기록만으로는).
    #   
    #   하지만, 우리는 각 건별 R (r_multiple)을 이미 '불변 분모'로 계산해서 저장했습니다.
    #   즉, r_multiple = (Exit - Entry) / (Entry - InitialStop)
    #   
    #   R_total = (P_total) / (Risk_total)
    #   P_total = Sum( P_i )
    #   Risk_total = (Entry - InitStop) * Qty_total
    #
    #   이 방식은 Entry와 InitStop이 단일 TradeID 내에서 '불변'이라는 가정 하에 성립합니다.
    #   또한 r_multiple_i = P_i / (Entry - InitStop) 이므로,
    #   P_i = r_multiple_i * (Entry - InitStop)
    #
    #   따라서,
    #   Total R = Sum( P_i ) / Risk_total 
    #           = Sum( r_i * (E-S)_unit ) / ( (E-S)_unit * Qty_total )
    #           = Sum( r_i ) / Qty_total ?? -> 아니죠.
    #   
    #   Wait. 
    #   개별 r_multiple = (Exit - Entry) / Unit_Risk
    #   여기서 Unit_Risk = (Entry - Initial_Stop) (주당 리스크)
    #   
    #   우리가 원하는 최종 R = (Total Profit $) / (Total Risk $)
    #   Total Profit $ = Sum [ (Exit_Px - Entry_Px) * Exit_Qty ]
    #   Total Risk $ = Unit_Risk * Total_Qty
    #
    #   그런데 trade_history에는 Unit_Risk 정보가 명시적으로 컬럼에 없습니다. (계산되어 R로 들어감)
    #   하지만 역산할 수 있습니다. 
    #   Profit_i ($) = r_multiple_i * Unit_Risk * Exit_Qty_i ... (X) 
    #   아닙니다. r_multiple_i = (Exit_Px - Entry_Px) / Unit_Risk 이므로
    #   Line Profit ($) = (Exit_Px - Entry_Px) * Exit_Qty
    #                   = (r_multiple_i * Unit_Risk) * Exit_Qty
    #   
    #   Total Profit ($) = Sum [ r_multiple_i * Exit_Qty_i * Unit_Risk ]
    #                    = Unit_Risk * Sum [ r_multiple_i * Exit_Qty_i ]  (단, Unit_Risk가 일정하다면)
    #
    #   Total Risk ($) = Unit_Risk * Total_Qty
    #                  = Unit_Risk * Sum [ Exit_Qty_i ] (전량 청산되었다면)
    #
    #   Final Trade R = Total Profit / Total Risk
    #                 = (Unit_Risk * Sum[ r_i * q_i ]) / (Unit_Risk * Sum[ q_i ])
    #                 = Sum( r_i * q_i ) / Sum( q_i )
    #
    #   결론: "가중 평균 R" (Weighted Average R)이 "Total Profit / Total Risk" 와 수학적으로 동일합니다!
    #   증명:
    #     R_avg = (R1*Q1 + R2*Q2) / (Q1+Q2)
    #     = ( (P1/Risk_u)*Q1 + (P2/Risk_u)*Q2 ) / Q_total
    #     = (1/Risk_u) * (P1*Q1 + P2*Q2 ?! 아님. P1은 주당 수익이므로 P1*Q1은 총수익1)
    #     Wait. r_multiple 은 Price 차이 기준입니다.
    #     r = (Exit - Entry) / (Entry - Stop)
    #     Profit_dollar_1 = (Exit1 - Entry) * Q1 = r1 * (Entry - Stop) * Q1
    #     Profit_dollar_total = (Entry - Stop) * [ r1*Q1 + r2*Q2 + ... ]
    #     Risk_dollar_total = (Entry - Stop) * [ Q1 + Q2 + ... ]
    #     
    #     Final R = Profit_dollar_total / Risk_dollar_total
    #             = [ (E-S) * Sum(r_i * q_i) ] / [ (E-S) * Sum(q_i) ]
    #             = Sum(r_i * q_i) / Sum(q_i)
    #
    #   즉, "청산 수량(Exit Qty)으로 가중 평균한 R값"이 정확한 Total R입니다.
    #   기존 코드도 weighted_r = r * portion (portion = qty / total_qty) 이었으므로 
    #   논리적으로는 맞았어야 합니다.
    #   
    #   문제는 **개별 r_multiple 계산 시 분모(Risk Unit)**가 오락가락했다는 점입니다.
    #   이제 분모를 고정했으니, 기존의 가중평균 로직을 그대로 쓰면 됩니다.
    
    # 1. Trade_ID 별 총 수량 계산
    total_qty_per_trade = df.groupby('trade_id')['exit_qty'].transform('sum')
    df = df.copy()
    
    # 2. 가중치 계산 (해당 건이 전체 거래에서 차지하는 비중)
    df['weight'] = df['exit_qty'] / total_qty_per_trade
    df['weight'] = df['weight'].fillna(0)
    
    # 3. 기여 R 계산
    df['contribution_r'] = df['r_multiple'] * df['weight']
    
    # 4. Trade_ID 별 합산
    trade_grouped = df.groupby('trade_id').agg({
        'contribution_r': 'sum',
        'ticker': 'first',
        'exit_date': 'last'
    }).rename(columns={'contribution_r': 'total_trade_r'})
    
    total_trades = len(trade_grouped)
    winning_trades = trade_grouped[trade_grouped['total_trade_r'] > 0]
    
    win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0
    expectancy = trade_grouped['total_trade_r'].mean() if total_trades > 0 else 0
    
    return expectancy, win_rate, total_trades

# ============================================================================
# [3. CALCULATION FUNCTIONS]
# ============================================================================

def calculate_or_r(entry_price, stop_loss, quantity, current_1r_unit):
    """Open Risk (OR)를 R 단위로 계산"""
    or_amount = abs(entry_price - stop_loss) * quantity
    return or_amount / current_1r_unit if current_1r_unit > 0 else 0

def calculate_dynamic_or(entry_price, stop_loss, quantity, current_1r_unit):
    """실시간 스탑 가격을 반영한 동적 OR 계산"""
    # 스탑이 본전(BE) 위로 올라왔다면 리스크는 0으로 간주 (Risk-Free)
    if stop_loss >= entry_price:
        return 0.0
    
    or_amount = (entry_price - stop_loss) * quantity
    return or_amount / current_1r_unit if current_1r_unit > 0 else 0

def calculate_tor(portfolio_df, current_1r_unit):
    """Total Open Risk (TOR) 계산 - 동적 OR 사용"""
    if portfolio_df.empty:
        return 0.0
    
    portfolio_df['OR_R'] = portfolio_df.apply(
        lambda row: calculate_dynamic_or(
            row['entry_price'], row['stop_loss'], 
            row['quantity'], current_1r_unit
        ), axis=1
    )
    return portfolio_df['OR_R'].sum()

def get_regime_params(regime):
    """
    시장 국면별 리스크 파라미터 (Darvas의 조정장 프로토콜 반영)
    
    설계 원칙:
    - TOR Limit: 동시 진행 가능한 총 열린 리스크 (계좌 전체)
    - R Multiplier: 개별 포지션의 판돈 조절 (BASE_1R_PCT에 곱해짐)
    
    Yellow/Red 국면에서 R을 줄이는 이유:
    1. 변동성 확대 시 슬리피지 증가 → 실효 손절이 계획보다 커짐
    2. 승률 저하 환경에서 판돈을 줄여 드로다운 시간 단축
    3. TOR 제한만으로는 빈도를 줄일 뿐, 개별 타격의 강도는 제어 못함
    """
    params = {
        "GREEN": {
            "tor_limit": 5.0, 
            "r_multiplier": 1.0, 
            "color": "#00c864", 
            "desc": "정상 운용: 공격적 실행 (Full Speed)"
        },
        
        "YELLOW": {
            "tor_limit": 3.0, 
            "r_multiplier": 0.5,  # 판돈 50% 감속 (0.5% R로 축소)
            "color": "#ffaa00", 
            "desc": "경계 모드: 판돈 및 빈도 동시 감속 (Half Speed)"
        },
        
        "RED": {
            "tor_limit": 1.0, 
            "r_multiplier": 0.25,  # 판돈 75% 감속 (0.25% R로 축소)
            "color": "#ff3232", 
            "desc": "생존 모드: 현금 비중 최대화 (Survival Only)"
        }
    }
    return params.get(regime, params["GREEN"])

def suggest_market_regime():
    """SPY와 RSP 데이터를 분석하여 국면을 제안"""
    try:
        # SPY와 RSP 데이터 호출 (최근 30일)
        spy = yf.Ticker("SPY").history(period="30d")
        rsp = yf.Ticker("RSP").history(period="30d")
        
        if spy.empty or rsp.empty:
            return "UNKNOWN", "gray"

        # 20 SMA 계산
        spy['SMA20'] = spy['Close'].rolling(window=20).mean()
        rsp['SMA20'] = rsp['Close'].rolling(window=20).mean()
        
        spy_curr = spy['Close'].iloc[-1]
        spy_sma = spy['SMA20'].iloc[-1]
        rsp_curr = rsp['Close'].iloc[-1]
        rsp_sma = rsp['SMA20'].iloc[-1]
        
        # 국면 판단 로직
        if spy_curr > spy_sma and rsp_curr > rsp_sma:
            return "GREEN", "#00c864"
        elif spy_curr < spy_sma and rsp_curr < rsp_sma:
            return "RED", "#ff3232"
        else:
            return "YELLOW", "#ffaa00"
    except Exception as e:
        return "ERROR", "gray"

def check_5day_rule(ticker, entry_date_str):
    """
    D0(진입일) 기준, 실제 거래일(Trading Days) 5개가 지났는지 확인
    D0은 진입일이고, D1~D5까지 5개 거래일을 의미
    """
    try:
        # DB에 저장된 entry_date는 'YYYY-MM-DD' 형식
        entry_dt = datetime.strptime(entry_date_str, '%Y-%m-%d')
        
        # yfinance로 진입일부터 오늘까지의 데이터 호출
        # (주의: yfinance의 start는 해당 날짜를 포함함)
        hist = yf.Ticker(ticker).history(start=entry_dt, interval="1d")
        
        # 봉의 개수가 1개면 D0(진입일 당일)
        # 봉의 개수가 6개면 D0 + 5개 거래일(D1~D5)이 경과한 상태
        trading_days_count = len(hist) 
        
        return trading_days_count
    except:
        return 0

def get_recent_performance(limit=5):
    """
    최근 N개 매매 기록의 승률 계산
    - 기록 부족 시 100% 반환 (패널티 없음, 초기 단계 보호)
    - 승률 저조 시 피드백 루프에서 RED 강제 전환 트리거
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        df_h = pd.read_sql_query(
            f"SELECT r_multiple FROM trade_history ORDER BY exit_date DESC LIMIT {limit}", 
            conn
        )
        if len(df_h) < limit:
            return 100.0  # 데이터 부족 시 중립 처리
        
        win_rate = (df_h['r_multiple'] > 0).mean() * 100
        return win_rate
    except:
        return 100.0
    finally:
        conn.close()


def suggest_market_regime(checklist_count, recent_win_rate):
    """
    하이브리드 리스크 프레임워크
    
    판단 계층:
    1. Index Position (SPY/RSP 20SMA 기준)
    2. Feedback Loop (최근 승률 < 20% → 강제 RED)
    3. Behavior Checklist (3개 이상 체크 시 강등)
    
    Returns:
        tuple: (regime, color, reason)
    """
    try:
        # 지수 데이터 호출 (60일간)
        spy = yf.Ticker("SPY").history(period="60d")
        rsp = yf.Ticker("RSP").history(period="60d")
        
        if spy.empty or rsp.empty:
            return "UNKNOWN", "gray", "데이터 조회 실패"
        
        spy['SMA20'] = spy['Close'].rolling(window=20).mean()
        rsp['SMA20'] = rsp['Close'].rolling(window=20).mean()
        
        spy_curr, spy_sma = spy['Close'].iloc[-1], spy['SMA20'].iloc[-1]
        rsp_curr, rsp_sma = rsp['Close'].iloc[-1], rsp['SMA20'].iloc[-1]
        
        # Layer 1: 기본 국면 (지수 기반)
        if spy_curr > spy_sma and rsp_curr > rsp_sma:
            base_regime = "GREEN"
        elif spy_curr < spy_sma and rsp_curr < rsp_sma:
            base_regime = "RED"
        else:
            base_regime = "YELLOW"
        
        # Layer 2: 피드백 루프 (성과 기반 강제 전환)
        if recent_win_rate < 20.0:
            return "RED", "#ff3232", "⚠️ 최근 승률 저조 (Survival Mode)"
        
        # Layer 3: 행동 가중치 (체크리스트 기반 강등)
        final_regime = base_regime
        reason = "지수 및 추세 양호"
        
        if checklist_count >= 3:
            if base_regime == "GREEN":
                final_regime = "YELLOW"
                reason = f"지수는 높으나 시장 행동 불안정 ({checklist_count}개 경고)"
            elif base_regime == "YELLOW":
                final_regime = "RED"
                reason = f"시장 행동 위험 수준 ({checklist_count}개 경고)"
            else:  # 이미 RED인 경우
                reason = f"지수 하락 + 시장 행동 악화 ({checklist_count}개 경고)"
        
        colors = {"GREEN": "#00c864", "YELLOW": "#ffaa00", "RED": "#ff3232"}
        return final_regime, colors.get(final_regime, "gray"), reason
        
    except Exception as e:
        return "ERROR", "gray", f"분석 실패: {str(e)}"

# ============================================================================
# [4. STREAMLIT UI INITIALIZATION]
# ============================================================================

st.set_page_config(layout="wide", page_title="R-Risk Manager OS")
init_db()

# ============================================================================
# [5. SIDEBAR: MARKET REGIME & ACCOUNT SETTINGS]
# ============================================================================

with st.sidebar:
    # 데이터 갱신 버튼
    if st.button("🔄 실시간 데이터 갱신", width='stretch'):
        st.cache_data.clear()
        st.rerun()
    
    st.divider()
    
    # ========== 행동 체크리스트 (정성적 신호) ==========
    st.header("🚦 Market Behavior Checklist")
    st.caption("Darvas의 시장 행동 분석 프레임워크")
    
    check_items = st.multiselect(
        "현재 관찰되는 시장 징후 선택",
        [
            "돌파 시도가 자주 실패 (Breakout Failure)",
            "리더급 종목에서 분산 캔들 출현",
            "지수 반등 후 Follow-Through 부재",
            "섹터 단위 동반 하락 (Sector Rotation Chaos)",
            "연속 손절로 인한 리듬 붕괴 (Personal)"
        ],
        help="3개 이상 선택 시 국면 강등이 발생합니다."
    )
    
    checklist_count = len(check_items)
    if checklist_count >= 3:
        st.warning(f"⚠️ {checklist_count}개 경고 → 국면 강등 가능성")
    
    st.divider()
    
    # ========== 성과 피드백 (정량적 신호) ==========
    st.header("📊 Performance Feedback")
    recent_win_rate = get_recent_performance(limit=5)
    
    if recent_win_rate < 20.0:
        st.error(f"🔴 최근 5회 승률: **{recent_win_rate:.1f}%** (위험)")
    elif recent_win_rate < 40.0:
        st.warning(f"🟡 최근 5회 승률: **{recent_win_rate:.1f}%** (주의)")
    else:
        st.success(f"🟢 최근 5회 승률: **{recent_win_rate:.1f}%**")
    
    st.divider()
    
    # ========== 하이브리드 국면 판단 ==========
    st.header("🤖 System Recommendation")
    suggested_regime, s_color, s_reason = suggest_market_regime(checklist_count, recent_win_rate)
    
    st.markdown(
        f"권장 국면: <b style='color:{s_color}; font-size:20px;'>{suggested_regime}</b>", 
        unsafe_allow_html=True
    )
    st.caption(f"📌 {s_reason}")
    
    # 세션 상태 초기화
    if 'regime_choice' not in st.session_state:
        st.session_state['regime_choice'] = None
    
    if st.button("🔄 추천 국면 자동 적용", width='stretch', type="primary"):
        st.session_state['regime_choice'] = suggested_regime
        st.toast(f"{suggested_regime} 국면으로 동기화되었습니다.")
        st.rerun()
    
    st.divider()
    
    # ========== 수동 오버라이드 옵션 ==========
    st.header("⚙️ Manual Override")
    st.caption("시스템 권장을 무시하고 수동 설정 가능")
    
    # 추천 국면이 적용되었는지 확인
    default_index = 0
    if st.session_state.get('regime_choice'):
        regime_options = ["GREEN", "YELLOW", "RED"]
        if st.session_state['regime_choice'] in regime_options:
            default_index = regime_options.index(st.session_state['regime_choice'])
    
    regime = st.radio(
        "현재 적용할 리스크 레벨",
        ["GREEN", "YELLOW", "RED"],
        index=default_index,
        help="수동 선택 시 자동 추천이 무시됩니다."
    )
    
    # 국면별 파라미터 설정
    regime_params = get_regime_params(regime)
    tor_limit = regime_params["tor_limit"]
    r_multiplier = regime_params["r_multiplier"]
    regime_color = regime_params["color"]
    
    st.divider()
    
    # ========== 계좌 정보 ==========
    st.header("💰 Account Configuration")
    # DB에서 현재 자산 로드
    current_stored_equity = get_total_equity()
    
    # 1. 입출금 및 자산 조정 팝오버
    with st.popover("💸 입출금 및 자산 조정"):
        adj_amount = st.number_input("조정 금액 (+입금 / -출금)", value=0.0, step=100.0)
        if st.button("자산 반영 실행"):
            adjust_equity_by_amount(adj_amount)
            st.success(f"${adj_amount:,.2f} 자산 반영 완료")
            st.rerun()
            
        st.divider()
        manual_equity = st.number_input("총 자산 강제 설정", value=current_stored_equity)
        if st.button("강제 설정 저장"):
            update_total_equity(manual_equity)
            st.rerun()
            
    # 2. 결과 표시 (Metric)
    total_equity = current_stored_equity
    st.metric("Total Equity", f"${total_equity:,.2f}")
    
    # 국면 반영 1R 계산
    current_1r_pct = BASE_1R_PCT * r_multiplier
    current_1r_unit = total_equity * current_1r_pct
    
    st.metric(
        label=f"Active 1R Unit ({regime})", 
        value=f"${current_1r_unit:,.2f}", 
        delta=f"{(current_1r_pct*100):.2f}% of Equity"
    )
    
    # 국면별 경고 메시지
    if regime == "RED":
        st.error("🔴 **RED ALERT**: 현금 비중 60% 이상 권장. 신규 진입 극도로 제한.")
    elif regime == "YELLOW":
        st.warning("🟡 **CAUTION**: 선별적 진입. High Conviction Only.")
    else:
        st.info("🟢 **ALL CLEAR**: 정상 운용 모드.")

# ============================================================================
# [6. MAIN DASHBOARD: RISK ENGINE & TOR TRACKER]
# ============================================================================

st.title("🚀 Risk OS Terminal")

col_risk, col_tor = st.columns([1, 1])

# --- [6-1. Hybrid Risk Engine: R-Based + Position Cap] ---
with col_risk:
    st.subheader("🛡️ Hybrid Risk Engine")
    st.caption("R-Based Sizing + Physical Concentration Limit")
    
    # ========== 현재 포트폴리오 상태 로드 (TOR 계산용) ==========
    df_portfolio_for_risk = get_portfolio()
    if not df_portfolio_for_risk.empty:
        current_tor = calculate_tor(df_portfolio_for_risk.copy(), current_1r_unit)
    else:
        current_tor = 0.0
    
    # 현재 국면 기반 Active 1R 계산
    active_r_pct = BASE_1R_PCT * r_multiplier
    active_1r_unit = total_equity * active_r_pct
    
    # 국면 상태 표시
    st.markdown(
        f"**Active Regime:** <span style='color:{regime_color}; font-size:18px;'>{regime}</span>", 
        unsafe_allow_html=True
    )
    regime_desc = get_regime_params(regime)["desc"]
    st.caption(f"📌 {regime_desc}")
    
    col_r1, col_r2 = st.columns(2)
    col_r1.metric("Current 1R", f"${active_1r_unit:,.0f}", delta=f"{active_r_pct*100:.2f}%")
    col_r2.metric("TOR Limit", f"{tor_limit} R")
    
    st.divider()
    
    # ========== 포지션 사이징 입력 ==========
    entry_p = st.number_input("Entry Price ($)", value=100.0, min_value=0.01, step=0.01, key="entry_price_v2")
    stop_p = st.number_input("Stop Loss Price ($)", value=95.0, min_value=0.01, step=0.01, key="stop_loss_v2")
    
    if entry_p > stop_p and entry_p > 0:
        stop_dist = entry_p - stop_p
        stop_dist_pct = (stop_dist / entry_p) * 100
        
        # ========== 계산 로직 ==========
        # 1) R-Based Theoretical Size
        theoretical_shares = int(active_1r_unit / stop_dist)
        theoretical_mag = theoretical_shares * entry_p
        theoretical_mag_pct = (theoretical_mag / total_equity) * 100
        
        # 2) Position Cap-Based Max Size (물리적 상한)
        max_cap_dollars = total_equity * MAX_POS_SIZE_PCT
        max_cap_shares = int(max_cap_dollars / entry_p)
        
        # 3) Final Decision: min(Theory, Cap)
        final_shares = min(theoretical_shares, max_cap_shares)
        final_mag = final_shares * entry_p
        final_mag_pct = (final_mag / total_equity) * 100
        final_or_r = (final_shares * stop_dist) / active_1r_unit  # active_1r_unit 사용
        
        # ========== UI 출력 ==========
        st.success("✅ Position Sizing Complete")
        
        col_out1, col_out2, col_out3 = st.columns(3)
        col_out1.metric("권장 수량", f"{final_shares:,} 주", help="R-Based와 Cap 중 작은 값")
        col_out2.metric("투입 금액", f"${final_mag:,.0f}", delta=f"{final_mag_pct:.1f}%")
        col_out3.metric("Stop 폭", f"{stop_dist_pct:.2f}%", delta=f"${stop_dist:.2f}")
        
        # ========== 경고 및 안내 ==========
        # Case 1: Cap 제한 발동
        if theoretical_shares > max_cap_shares:
            st.warning(
                f"⚠️ **Position Cap 적용됨**\n\n"
                f"- 이론적 수량: {theoretical_shares:,}주 (${theoretical_mag:,.0f}, {theoretical_mag_pct:.1f}%)\n"
                f"- Cap 제한: {max_cap_shares:,}주 (${max_cap_dollars:,.0f}, {MAX_POS_SIZE_PCT*100:.0f}%)\n\n"
                f"**근거:** 손절폭이 좁아({stop_dist_pct:.2f}%) 물리적 집중 리스크 발생. "
                f"슬리피지 발생 시 실효 손실이 계획 R을 초과할 가능성."
            )
        
        # Case 2: TOR 여유 부족
        remaining_tor = tor_limit - current_tor
        if final_or_r > remaining_tor:
            st.error(
                f"🚫 **TOR 초과 경고**\n\n"
                f"- 이 포지션 진입 시 점유: **{final_or_r:.2f} R**\n"
                f"- 현재 TOR 여유: **{remaining_tor:.2f} R**\n\n"
                f"**조치 필요:** 기존 포지션 일부 청산 또는 진입 보류"
            )
        else:
            st.info(f"📊 이 포지션 진입 시 TOR 점유: **{final_or_r:.2f} R** (여유: {remaining_tor:.2f} R)")
        
        # ========== R-Profit 목표가 계산 ==========
        st.divider()
        st.write("**🎯 R-Multiple Targets**")
        targets = {
            "1R": entry_p + stop_dist,
            "2R": entry_p + (stop_dist * 2),
            "3R": entry_p + (stop_dist * 3)
        }
        
        col_t1, col_t2, col_t3 = st.columns(3)
        for col, (label, price) in zip([col_t1, col_t2, col_t3], targets.items()):
            pct_gain = ((price - entry_p) / entry_p) * 100
            col.metric(label, f"${price:.2f}", delta=f"+{pct_gain:.1f}%")
        
    elif entry_p <= stop_p and entry_p != 0:
        st.error("❌ Stop Loss는 Entry Price보다 낮아야 합니다.")
    else:
        st.info("Entry Price와 Stop Loss를 입력하세요.")

# --- [6-2. TOR Tracker] ---
with col_tor:
    st.subheader("📊 TOR Tracker")
    
    # 실제 포트폴리오 데이터 로드
    df_portfolio = get_portfolio()
    
    if not df_portfolio.empty:
        # TOR 계산
        current_tor = calculate_tor(df_portfolio.copy(), current_1r_unit)
        risk_space = tor_limit - current_tor
        
        c1, c2 = st.columns(2)
        c1.metric("Current TOR", f"{current_tor:.2f} R", delta_color="inverse")
        c2.metric("Risk Space", f"{risk_space:.2f} R", delta=f"Limit: {tor_limit}R")
        
        # 섹터 집중도 경고
        if 'sector' in df_portfolio.columns:
            tech_count = df_portfolio[df_portfolio['sector'].str.contains("Tech", na=False)].shape[0]
            if tech_count >= 3:
                st.warning(f"🔥 테마 집중 리스크: Tech 섹터 {tech_count}개 종목 보유 중")
    else:
        st.info("포트폴리오가 비어있습니다.")
        current_tor = 0.0
        risk_space = tor_limit

# ============================================================================
# [7. PORTFOLIO MANAGEMENT]
# ============================================================================

st.divider()
st.subheader("📝 Portfolio Management")

# --- [7-1. 포지션 추가 폼] ---
with st.expander("➕ 새 포지션 추가"):
    with st.form("add_form"):
        col1, col2, col3 = st.columns(3)
        new_ticker = col1.text_input("Ticker", value="").upper()
        new_entry = col2.number_input("Entry Price", format="%.2f", value=100.0)
        new_stop = col3.number_input("Stop Loss", format="%.2f", value=95.0)
        
        col4, col5 = st.columns(2)
        new_qty = col4.number_input("Quantity", step=1, value=1)
        new_sector = col5.selectbox(
            "Sector", 
            ["Tech/AI", "Semiconductor", "IT", "Healthcare", "Consumer", "Industrials", "Consumer Staples", "Utilities", "Real Estate", "Materials", "Finance", "Energy", "Others"]
        )
        
        if st.form_submit_button("Add to Database"):
            if new_ticker:
                add_position(new_ticker, new_entry, new_stop, new_qty, new_sector)
                st.rerun()
            else:
                st.error("Ticker를 입력하세요.")

# --- [7-2. 실시간 포트폴리오 모니터링] ---
df_portfolio = get_portfolio()

if not df_portfolio.empty:
    st.subheader("📊 Live Portfolio Monitor")
    
    # 실시간 가격 및 알림 업데이트
    prices = []
    alerts = []
    days_held = []
    
    for _, row in df_portfolio.iterrows():
        # 현재가 및 차트 데이터 조회
        cp = get_current_price(row['ticker'])
        hist = yf.Ticker(row['ticker']).history(period="20d")
        sma20 = hist['Close'].rolling(20).mean().iloc[-1] if len(hist) >= 20 else None
        current_low = hist['Low'].iloc[-1] if not hist.empty else None
        
        prices.append(cp)
        
        # +1R 도달 알림 및 BE 업데이트 로직
        r_dist = abs(row['entry_price'] - row['stop_loss'])
        target_1r = row['entry_price'] + r_dist
        
        alert_msg = "Hold"
        if cp and cp >= target_1r:
            alert_msg = "⚠️ +1R Reached: Move Stop to BE"
        
        # 20SMA Undercut Logic (Persistence & Reset)
        if cp and sma20:
            if cp < sma20:
                # 처음 이탈한 경우 기준봉 저가 기록
                breakdown_low = row.get('breakdown_low')
                if breakdown_low is None or pd.isna(breakdown_low):
                    conn = sqlite3.connect(DB_PATH)
                    conn.cursor().execute(
                        "UPDATE portfolio SET breakdown_low = ? WHERE ticker = ?", 
                        (current_low, row['ticker'])
                    )
                    conn.commit()
                    conn.close()
                    st.info(f"🚨 {row['ticker']}: 20SMA 이탈. 기준 저가(${current_low:.2f}) 설정됨.")
                else:
                    # 이미 기준 저가가 있고, 이를 재이탈한 경우
                    if cp < breakdown_low:
                        alert_msg += " | ‼️ 기준 저가 붕괴! 즉시 청산 검토."
                        st.error(f"‼️ {row['ticker']}: 기준 저가(${breakdown_low:.2f}) 붕괴! 즉시 청산 검토.")
            else:
                # 20SMA 위로 복구한 경우 기준 저가 리셋
                breakdown_low = row.get('breakdown_low')
                if breakdown_low is not None and not pd.isna(breakdown_low):
                    conn = sqlite3.connect(DB_PATH)
                    conn.cursor().execute(
                        "UPDATE portfolio SET breakdown_low = NULL WHERE ticker = ?", 
                        (row['ticker'],)
                    )
                    conn.commit()
                    conn.close()
                    st.success(f"✨ {row['ticker']}: 20SMA 복구 완료. 리스크 리셋.")
        
        # 5일 규칙 알림 (실제 거래일 기준)
        try:
            trading_days_count = check_5day_rule(row['ticker'], row['entry_date'])
            # 캘린더 일수 계산 (표시용)
            ed = datetime.strptime(row['entry_date'], '%Y-%m-%d')
            calendar_days = (datetime.now() - ed).days
            days_held.append(calendar_days)
            
            # D0(1) + D1~D5(5) = 총 6개의 봉이면 5일 규칙 경과
            if trading_days_count >= 6:
                alert_msg += " | ⏳ 5-Day Rule: Partial Exit (D5 Passed)"
        except:
            days_held.append(0)
        
        alerts.append(alert_msg)
    
    # 데이터프레임 업데이트
    df_portfolio['Current Price'] = prices
    df_portfolio['Days Held'] = days_held
    df_portfolio['Alerts'] = alerts
    
    # TOR 계산 및 표시 (동적 OR 사용)
    current_tor = calculate_tor(df_portfolio.copy(), current_1r_unit)
    df_portfolio['OR_R'] = df_portfolio.apply(
        lambda row: calculate_dynamic_or(
            row['entry_price'], row['stop_loss'], 
            row['quantity'], current_1r_unit
        ), axis=1
    )
    
    # 스타일링된 데이터프레임 표시
    display_cols = ['ticker', 'entry_date', 'entry_price', 'stop_loss', 'quantity', 'OR_R', 'sector', 'Current Price', 'Days Held', 'Alerts']
    available_cols = [col for col in display_cols if col in df_portfolio.columns]
    
    st.dataframe(
        df_portfolio[available_cols].style.map(
            lambda x: 'background-color: #ffcccc' if '⚠️' in str(x) or '‼️' in str(x) else '', 
            subset=['Alerts']
        ),
        column_config={
            "entry_price": st.column_config.NumberColumn("Entry Price", format="%.3f"),
            "stop_loss": st.column_config.NumberColumn("Stop Loss", format="%.3f"),
            "OR_R": st.column_config.NumberColumn("OR (R)", format="%.3f R"),
            "Current Price": st.column_config.NumberColumn("Current Price", format="%.3f"),
        }
    )
    
    st.metric("Total Open Risk (TOR)", f"{current_tor:.2f} R")
    
    # --- [7-2-1. Active Position Management] ---
    st.divider()
    st.subheader("🛠️ Active Position Management")
    
    for _, row in df_portfolio.iterrows():
        with st.expander(f"⚙️ {row['ticker']} 관리 (Entry: ${row['entry_price']:.2f}, Stop: ${row['stop_loss']:.2f})"):
            col1, col2, col3 = st.columns(3)
            
            # 1) Move Stop to BE (리스크 제거)
            with col1:
                if st.button(f"🎯 Move to BE", key=f"btn_be_{row['ticker']}", width='stretch'):
                    update_stop_loss(row['ticker'], row['entry_price'])
                    st.success(f"{row['ticker']} 스탑을 본전(${row['entry_price']:.2f})으로 상향하여 OR을 0으로 조정했습니다.")
                    st.rerun()
            
            # 2) D5 Partial Exit (분할 매도)
            with col2:
                with st.popover("✂️ Partial Exit", width='stretch'):
                    current_price = get_current_price(row['ticker'])
                    default_exit_price = current_price if current_price else row['entry_price']
                    
                    exit_qty = st.number_input(
                        "청산 수량", 
                        value=max(1, int(row['quantity']/2)), 
                        min_value=1,
                        max_value=row['quantity'],
                        step=1,
                        key=f"exit_qty_{row['ticker']}"
                    )
                    exit_px = st.number_input(
                        "청산 가격", 
                        value=float(default_exit_price),
                        format="%.2f",
                        key=f"exit_px_{row['ticker']}"
                    )
                    if st.button("Confirm Partial Exit", key=f"confirm_partial_{row['ticker']}"):
                        process_partial_exit(row['ticker'], exit_qty, exit_px, row['entry_price'], current_1r_unit)
                        st.rerun()
            
            # 3) Current Status Display
            with col3:
                current_or = calculate_dynamic_or(row['entry_price'], row['stop_loss'], row['quantity'], current_1r_unit)
                st.metric("Current OR", f"{current_or:.2f} R", 
                         delta="Risk-Free" if current_or == 0 else None,
                         delta_color="normal" if current_or == 0 else "off")
    
    # --- [7-3. 포지션 삭제/청산] ---
    col_delete, col_close = st.columns(2)
    
    with col_delete:
        st.subheader("🗑️ 포지션 삭제")
        target_ticker = st.selectbox("삭제할 종목 선택", df_portfolio['ticker'].tolist(), key="delete_ticker")
        if st.button("포지션 삭제 (DB에서 제거)"):
            delete_position(target_ticker)
            st.success(f"{target_ticker} 포지션이 삭제되었습니다.")
            st.rerun()
    
    with col_close:
        st.subheader("🚪 포지션 청산")
        ticker_to_close = st.selectbox("청산할 종목", df_portfolio['ticker'].tolist(), key="close_ticker")
        
        # 선택된 종목 정보 가져오기
        sel_row = df_portfolio[df_portfolio['ticker'] == ticker_to_close].iloc[0]
        current_qty = int(sel_row['quantity'])
        current_price = get_current_price(ticker_to_close)
        default_exit = current_price if current_price else sel_row['entry_price']
        
        # UI 입력 (수량, 가격)
        c_qty, c_prc = st.columns(2)
        qty_to_close = c_qty.number_input("청산 수량", min_value=1, max_value=current_qty, value=current_qty, step=1, key="close_qty_input")
        exit_p = c_prc.number_input("청산 가격", value=float(default_exit), format="%.2f", key="close_price_input")
        
        if st.button("청산 실행 (성적표 이동)"):
            close_position(ticker_to_close, exit_p, qty_to_close)
            st.success(f"{ticker_to_close} {qty_to_close}주 청산 처리되었습니다.")
            st.rerun()

else:
    st.info("포트폴리오가 비어있습니다. 새 포지션을 추가하세요.")

# ============================================================================
# [8. PERFORMANCE SCORECARD]
# ============================================================================

st.divider()
st.subheader("📈 Performance Scorecard (Expectancy)")

conn_h = get_db_connection()
df_h = pd.read_sql_query("SELECT * FROM trade_history ORDER BY exit_date DESC", conn_h)
conn_h.close()

if not df_h.empty:
    # 1) 통계 계산 섹션 (분할 매도 반영 Logic)
    expectancy, win_rate, total_trades_count = calculate_real_expectancy(df_h)
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Win Rate", f"{win_rate:.1f}%")
    c2.metric("Expectancy", f"{expectancy:.2f} R")
    c3.metric("Total Trades", f"{total_trades_count}", help="Aggregated by TradeID")

    st.write("---")
    st.write("**매매 기록 관리 (수정하려면 셀을 더블 클릭하세요)**")

    # 2) 체크박스 컬럼 추가
    # 데이터프레임 맨 앞에 '선택' 컬럼 추가
    df_h.insert(0, "선택", False)
    
    # 3) 데이터 에디터 출력 (수정 가능)
    edited_df = st.data_editor(
        df_h,
        column_config={
            "선택": st.column_config.CheckboxColumn("선택", default=False),
            "trade_id": st.column_config.TextColumn("Trade ID", disabled=True),
            "ticker": st.column_config.TextColumn("Ticker"),
            "entry_date": st.column_config.TextColumn("Entry Date"),
            "exit_date": st.column_config.TextColumn("Exit Date"),
            "entry_price": st.column_config.NumberColumn("Entry Price", format="%.2f"),
            "exit_price": st.column_config.NumberColumn("Exit Price", format="%.2f"),
            "exit_qty": st.column_config.NumberColumn("Exit Qty", step=1),
            "r_multiple": st.column_config.NumberColumn("R-Multiple", format="%.2f R")
        },
        disabled=["id", "trade_id"], # ID는 수정 불가
        hide_index=True,
        width='stretch'
    )
    
    # 4) 액션 버튼 (삭제 / 저장 / 초기화)
    selected_ids = edited_df[edited_df["선택"] == True]["id"].tolist()
    
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 2])
    
    # [삭제 버튼]
    if selected_ids:
        if col_btn1.button(f"🗑️ {len(selected_ids)}건 삭제", type="primary"):
            delete_selected_trades(selected_ids)
            st.toast(f"{len(selected_ids)}건의 기록이 삭제되었습니다.")
            st.rerun()
            
    # [저장 버튼]
    if col_btn2.button("💾 변경 사항 저장"):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            for index, row in edited_df.iterrows():
                cursor.execute("""
                    UPDATE trade_history 
                    SET ticker = ?, entry_date = ?, exit_date = ?, 
                        entry_price = ?, exit_price = ?, r_multiple = ?, exit_qty = ?
                    WHERE id = ?
                """, (
                    row['ticker'], row['entry_date'], row['exit_date'], 
                    row['entry_price'], row['exit_price'], row['r_multiple'], row.get('exit_qty', 1),
                    row['id']
                ))
            conn.commit()
            conn.close()
            st.success("✅ 데이터가 성공적으로 수정되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"저장 중 오류 발생: {e}")

    # [초기화 버튼]
    if col_btn3.button("⚠️ 전체 초기화"):
        if st.checkbox("정말로 모든 데이터를 삭제하시겠습니까?"):
            conn = get_db_connection()
            conn.cursor().execute("DELETE FROM trade_history")
            conn.commit()
            conn.close()
            st.rerun()
else:
    st.info("아직 매매 기록이 없습니다.")

# ============================================================================
# [9. EXIT ENGINE VISUALIZER (Optional)]
# ============================================================================

st.divider()
st.subheader("📉 Exit Engine & Trailing Monitor")

# 선택된 종목이 있을 경우 실제 데이터 사용
selected_ticker_for_chart = None
df_portfolio_chart = get_portfolio()  # 차트용 포트폴리오 재조회
if not df_portfolio_chart.empty:
    selected_ticker_for_chart = st.selectbox(
        "차트를 표시할 종목 선택", 
        ["None"] + df_portfolio_chart['ticker'].tolist(),
        key="chart_ticker"
    )

if selected_ticker_for_chart and selected_ticker_for_chart != "None":
    # 실제 종목 데이터 사용
    try:
        ticker_data = yf.Ticker(selected_ticker_for_chart)
        hist = ticker_data.history(period="3mo")
        
        if not hist.empty:
            df_chart = pd.DataFrame({
                'Date': hist.index,
                'Close': hist['Close'].values,
                'Low': hist['Low'].values
            })
            df_chart['SMA20'] = df_chart['Close'].rolling(20).mean()
            
            # 해당 종목의 포지션 정보 가져오기
            pos_info = df_portfolio_chart[df_portfolio_chart['ticker'] == selected_ticker_for_chart].iloc[0]
            entry_p_chart = pos_info['entry_price']
            stop_p_chart = pos_info['stop_loss']
            
            # 차트 생성
            fig = make_subplots(rows=1, cols=1)
            fig.add_trace(go.Scatter(x=df_chart['Date'], y=df_chart['Close'], 
                                    name="Price", line=dict(color="blue")))
            fig.add_trace(go.Scatter(x=df_chart['Date'], y=df_chart['SMA20'], 
                                    name="20 SMA", line=dict(color="orange")))
            
            # BE Stop Line (+1R 도달 시)
            r_dist_chart = abs(entry_p_chart - stop_p_chart)
            be_line = entry_p_chart
            fig.add_hline(y=be_line, line_dash="dash", line_color="gray",
                         annotation_text="BE Stop Line")
            
            # 20 SMA Undercut 감지
            latest_close = df_chart['Close'].iloc[-1]
            latest_sma = df_chart['SMA20'].iloc[-1]
            if latest_close < latest_sma:
                undercut_low = df_chart['Low'].iloc[-1]
                fig.add_hline(y=undercut_low, line_dash="dot", line_color="red",
                             annotation_text="Undercut Trigger (Exit if broken)")
                st.error(f"🚨 20 SMA 이탈 확인: {df_chart['Date'].iloc[-1].date()} "
                        f"저가(${undercut_low:.2f}) 재이탈 시 최종 매도")
            
            fig.update_layout(height=500, template="plotly_white", hovermode="x unified",
                            title=f"{selected_ticker_for_chart} Exit Engine Monitor")
            st.plotly_chart(fig, width='stretch')
        else:
            st.warning("데이터를 불러올 수 없습니다.")
    except Exception as e:
        st.error(f"차트 생성 중 오류 발생: {str(e)}")
else:
    st.info("포트폴리오에서 종목을 선택하면 Exit Engine 차트가 표시됩니다.")

st.caption("※ VCP 스크리너 기능은 리스크 관리 집중을 위해 현재 비활성화되어 있습니다.")

# ============================================================================
# [10. REAL MARKET DATA]
# ============================================================================

def get_ai_ready_data(ticker, use_adj_close=False, start_date=None):
    """
    AI 분석용 데이터 생성 함수
    - 최근 데이터 확보 (넉넉하게 200일)
    - OHLC, Volume, 20SMA, 20VMA 계산
    - 텍스트 포맷으로 변환
    :param use_adj_close: True일 경우 배당/분할이 모두 반영된 수정주가 사용 (Total Return)
                          False일 경우 차트와 동일한 주가 사용 (Split-Adjusted Only)
    :param start_date: 특정 날짜 이후 데이터만 필터링 (datetime.date or str)
    """
    try:
        # 1. 데이터 가져오기 (전체 데이터)
        # auto_adjust=True -> Dividends & Splits 반영 (Total Return)
        # auto_adjust=False -> Splits만 반영된 Yahoo Finance 'Close' (Chart Price)
        df = yf.Ticker(ticker).history(period="max", auto_adjust=use_adj_close)
        if df.empty:
            return None, "데이터를 불러올 수 없습니다."
            
        # 2. 기술적 지표 계산
        # 20SMA (Price)
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        # 20VMA (Volume)
        df['VMA20'] = df['Volume'].rolling(window=20).mean()
        
        # 3. 데이터 필터링 (Start Date)
        if start_date:
            # df.index는 datetime64[ns, America/New_York] 등의 timezone이 있을 수 있음
            # start_date를 pd.Timestamp로 변환 후 timezone-naive 비교 혹은 tz-localize 처리
            ts_start = pd.Timestamp(start_date).tz_localize(df.index.tz)
            df_recent = df[df.index >= ts_start].copy()
        else:
            df_recent = df.copy()
        
        # 4. 텍스트 포맷팅
        # 헤더
        output_txt = f"[{ticker} Daily Data (All Available Dates)]\n"
        output_txt += "Date | Open | High | Low | Close | Volume | 20SMA | 20VMA\n"
        output_txt += "-" * 80 + "\n"
        
        for date, row in df_recent.iterrows():
            date_str = date.strftime('%Y-%m-%d')
            sma20_str = f"{row['SMA20']:.3f}" if not pd.isna(row['SMA20']) else "NaN"
            vma20_str = f"{row['VMA20']:.0f}" if not pd.isna(row['VMA20']) else "NaN"
            
            line = (
                f"{date_str} | "
                f"{row['Open']:.3f} | "
                f"{row['High']:.3f} | "
                f"{row['Low']:.3f} | "
                f"{row['Close']:.3f} | "
                f"{row['Volume']:.0f} | "
                f"{sma20_str} | "
                f"{vma20_str}"
            )
            output_txt += line + "\n"
            
        return output_txt, None
        
    except Exception as e:
        return None, str(e)

st.divider()
st.subheader("📊 Real Market Data")
st.caption("AI(Gemini 등)에게 붙여넣기 좋은 형식으로 주가 데이터를 생성합니다.")

with st.expander("데이터 생성기 열기"):
    col_ai_1, col_ai_2 = st.columns([1, 1])
    
    with col_ai_1:
        # 종목 선택: 포트폴리오 종목 or 직접 입력
        idx_options = ["직접 입력"]
        if not df_portfolio.empty:
             idx_options += df_portfolio['ticker'].tolist()
             
        ai_ticker_select = st.selectbox("종목 선택", idx_options, key="ai_ticker_select")
        
        if ai_ticker_select == "직접 입력":
            ai_ticker_input = st.text_input("Ticker 입력", value="TSLA", key="ai_ticker_input").upper()
            target_ticker = ai_ticker_input
        else:
            target_ticker = ai_ticker_select
            
    with col_ai_2:
        st.write("") # Spacer
        # TradingView 등 대부분의 차트는 배당락이 반영된 수정주가를 기본으로 사용함
        use_total_return = st.checkbox("배당락/액면분할 반영 (Adjusted)", value=True, help="체크 시 트레이딩뷰/HTS와 동일하게 배당락이 반영된 수정주가를 사용합니다. (체크 해제 시 당시 체결가)")
        
        # Start Date Input (Optional)
        use_start_date = st.checkbox("시작 날짜 지정 (TradingView 등과 일치시키기 위함)", value=False)
        start_date_val = None
        if use_start_date:
            start_date_val = st.date_input("시작 날짜 선택", value=pd.to_datetime("2024-05-03"))

        if st.button("Generate Data 📄", key="btn_gen_ai_data"):
            if target_ticker:
                data_txt, err = get_ai_ready_data(target_ticker, use_adj_close=use_total_return, start_date=start_date_val)
                if data_txt:
                    st.session_state['ai_data_output'] = data_txt
                else:
                    st.error(f"오류 발생: {err}")
            else:
                st.warning("Ticker를 입력하세요.")

    if 'ai_data_output' in st.session_state:
        st.text_area("결과 데이터 (복사해서 사용하세요)", st.session_state['ai_data_output'], height=300)

# ============================================================================
# [11. VOLUME SPIKE SCREENER]
# ============================================================================


# ============================================================================
# [11. VOLUME SPIKE SCREENER (Enhanced)]
# ============================================================================


def get_volume_spike_tickers(ticker_list, threshold_ratio=2.0, enforce_sma200=True):
    """
    조건:
    1. 거래량 > 20VMA * threshold_ratio
    2. (Optional) 현재가 > 200SMA (강세장 필터)
    3. 전일대비 상승 마감 (Positive Change)
    4. OTC 종목 제외 (Heuristic)
    """
    # 중복 제거 & OTC 필터링 (5글자이면서 F/Y/Q로 끝나는 경우 제외)
    ticker_list = list(set(ticker_list))
    filtered_list = []
    for t in ticker_list:
        # 간단한 OTC 필터: 5글자 이상이고 끝이 F, Y, Q 인 경우 (ADR, Foreign, Bankruptcy 등)
        if len(t) >= 5 and t[-1] in ['F', 'Y', 'Q']:
            continue
        filtered_list.append(t)
        
    if not filtered_list:
        return []

    # 1. 데이터 일괄 다운로드 (200SMA 계산을 위해 1년치 필요)
    try:
        # progress=False, threads=True for speed
        period = "1y" if enforce_sma200 else "2mo"
        data = yf.download(filtered_list, period=period, group_by='ticker', progress=False, threads=True)
    except Exception as e:
        return []

    spike_tickers = []
    
    # 2. 각 티커별 분석
    is_single = (len(filtered_list) == 1)

    # 데이터가 비어있는 경우 조기 종료
    if data.empty:
        return []

    for ticker in filtered_list:
        try:
            if is_single:
                df = data
            else:
                try:
                    df = data[ticker]
                except KeyError:
                    continue

            # 결측치 제거
            df = df.dropna()
            
            # 최소 데이터 요구량 확인
            min_days = 200 if enforce_sma200 else 20
            if len(df) < min_days:
                continue

            # 3. 지표 계산
            close = df['Close']
            vol = df['Volume']
            
            # [조건 1] 전일 대비 상승 (Positive Change)
            # 최소 2일치 데이터 필요
            if len(close) < 2:
                continue
                
            prev_close = close.iloc[-2]
            curr_close = close.iloc[-1]
            
            if curr_close <= prev_close:
                continue # 전일 대비 하락하거나 보합이면 제외

            # [조건 2] 200 SMA (Trend Filter)
            if enforce_sma200:
                sma_200 = close.rolling(window=200).mean()
                if curr_close <= sma_200.iloc[-1]:
                    continue # 200일선 아래면 탈락

            # [조건 3] 20 VMA (Volume Filter)
            vma_20 = vol.rolling(window=20).mean()

            # 4. 조건 비교 (가장 최근 데이터)
            last_vol = vol.iloc[-1]
            last_vma = vma_20.iloc[-1]
            
            if last_vma > 0 and last_vol >= (last_vma * threshold_ratio):
                spike_tickers.append(ticker)

        except Exception:
            continue

    return spike_tickers

st.divider()
st.subheader("📢 Volume Spike Screener (Trend Aligned)")
st.caption("조건: 1) Price > 200SMA, 2) Volume > 20VMA x Ratio, 3) **Positive Change(전일비 상승)**, 4) No OTC")

with st.expander("Screener Settings & Run", expanded=True):
    col_scr_1, col_scr_2 = st.columns([1, 1])
    
    with col_scr_1:
        st.write("#### 📋 Target Watchlist")
        user_input_tickers = st.text_area(
            "티커 입력 (쉼표/공백 구분)", 
            "TSLA, NVDA, AMD, AAPL, MSFT, PLTR, SOXL, TQQQ",
            height=150
        )
        
        target_tickers = []
        if user_input_tickers:
            import re
            cleaned_input = re.sub(r'[\s,]+', ' ', user_input_tickers).strip()
            # 입력 시점에 OTC 필터링 미리 적용해서 보여줄 수도 있지만, 
            # 검색 함수 내부에서 처리하므로 여기서는 Raw List만 생성
            target_tickers = [t.upper() for t in cleaned_input.split(' ') if t]
            st.caption(f"총 {len(target_tickers)}개 입력됨")

    with col_scr_2:
        st.write("#### ⚙️ Settings")
        threshold_val = st.slider(
            "Volume Threshold (Ratio)", 
            min_value=1.5, 
            max_value=10.0, 
            value=2.0, 
            step=0.5,
            help="2.0 = 평소 대비 200% 거래량"
        )
        
        # 고정 조건 표시
        st.markdown("""
        **Fixed Conditions:**
        - ✅ **Price > 200 SMA** (Long-term Trend)
        - ✅ **Positive Change %** (Close > Prev Close)
        - ✅ **NO OTC** (Exclude 5-char ends with F/Y/Q)
        - ✅ **Volume > {:.0f}% of 20VMA**
        """.format(threshold_val*100))
        
        st.write("") # Spacer
        if st.button("🚀 Run Watchlist Scan", key="btn_vol_scan", width='stretch'):
            if not target_tickers:
                st.error("티커 리스트가 비어있습니다.")
            else:
                # 대량 검색 시 경고
                if len(target_tickers) > 100:
                    st.warning(f"⚠️ {len(target_tickers)}개 종목을 검색합니다. 시간이 다소 소요될 수 있습니다.")
                
                with st.spinner(f"Scanning {len(target_tickers)} tickers..."):
                    spikes = get_volume_spike_tickers(
                        target_tickers, 
                        threshold_ratio=threshold_val, 
                        enforce_sma200=True
                    )
                
                if spikes:
                    st.success(f"🔥 조건 만족 종목: {len(spikes)}개 찾음!")
                    st.markdown(f"### 🚨 {', '.join(spikes)}")
                else:
                    st.info(f"✅ 조건에 맞는 종목이 없습니다.")
