from flask import Flask, send_file
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import subprocess
import os
import pty
import select
import signal
import sys
import threading
import termios 
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'galaxy_secret_default')
# 允許所有來源連線 (這是為了讓 Google Sites 可以存取)
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*")

current_process = None
master_fd_global = None

def log(msg):
    print(f"[系統] {msg}", flush=True)

@app.route('/')
def home():
    # 這裡回傳 index.html 是為了方便你直接測試雲端網址
    # 但實際在 Google Sites 上，你是嵌入 HTML 代碼，不會用到這個路由
    try:
        return send_file('index.html')
    except Exception:
        return "Backend is running! (index.html not found, but socket is ready)"

@socketio.on('run_code_v2')
def handle_run_code(data):
    global current_process, master_fd_global
    
    code = data.get('code')
    lang = data.get('lang', 'cpp')
    
    log(f"收到執行請求 ({lang})...")
    
    # [雲端修正] 使用 /tmp 目錄，確保在容器內有寫入權限且不會殘留垃圾檔案
    # 使用 uuid 避免多人在同一瞬間執行時檔案互相覆蓋 (雖然這個簡單版還是單執行緒邏輯)
    session_id = str(uuid.uuid4())[:8]
    work_dir = "/tmp"
    
    if lang == 'python':
        source_file = os.path.join(work_dir, f"galaxy_{session_id}.py")
        run_cmd = ['python3', '-u', source_file]
    else: # cpp
        source_file = os.path.join(work_dir, f"galaxy_{session_id}.cpp")
        exe_file = os.path.join(work_dir, f"galaxy_{session_id}.out")
        run_cmd = [exe_file]

    # 1. 寫入檔案
    try:
        with open(source_file, "w", encoding='utf-8') as f:
            f.write(code)
    except Exception as e:
        emit('program_output', {'data': f"❌ 寫入失敗 (Server): {e}\n"})
        return

    # 2. 如果是 C++，需要編譯
    if lang == 'cpp':
        # [雲端修正] 改用 g++，因為在 Docker 容器中最容易取得
        compiler = 'g++'
        
        # 編譯指令: g++ source.cpp -o source.out
        compile_cmd = [compiler, source_file, '-o', exe_file]
        
        log(f"正在編譯: {' '.join(compile_cmd)}")
        compile_res = subprocess.run(compile_cmd, capture_output=True, text=True)

        if compile_res.returncode != 0:
            emit('program_output', {'data': f"❌ 編譯錯誤:\n{compile_res.stderr}"})
            emit('program_status', {'status': 'error'})
            # 清理檔案
            try: os.remove(source_file)
            except: pass
            return

    # 3. 啟動互動式執行 (使用 PTY)
    try:
        master_fd_global, slave_fd = pty.openpty()
        
        # 關閉回顯 (ECHO)，避免輸入的字元重複顯示在終端機
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        current_process = subprocess.Popen(
            run_cmd, 
            stdin=slave_fd, 
            stdout=slave_fd, 
            stderr=slave_fd,
            preexec_fn=os.setsid, 
            close_fds=True
        )
        os.close(slave_fd)
        
        emit('program_output', {'data': ""}) # 清空或初始化
        
        # 啟動執行緒讀取輸出
        t = threading.Thread(target=read_output, args=(master_fd_global, current_process, source_file, exe_file if lang=='cpp' else None))
        t.daemon = True
        t.start()
    except Exception as e:
        emit('program_output', {'data': f"啟動失敗: {str(e)}"})
        if master_fd_global: 
            try: os.close(master_fd_global) 
            except: pass

def read_output(fd, proc, src_file, exe_file):
    try:
        while True:
            # 使用 select 監聽輸出，超時設為 0.1 秒
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    data = os.read(fd, 1024)
                    if data: 
                        socketio.emit('program_output', {'data': data.decode(errors='ignore')})
                    else: 
                        break # EOF
                except OSError: 
                    break
            
            # 檢查程式是否結束
            if proc.poll() is not None:
                # 再次讀取剩餘的緩衝區
                try:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if fd in r:
                        data = os.read(fd, 1024)
                        if data: socketio.emit('program_output', {'data': data.decode(errors='ignore')})
                except: pass
                break
    except Exception as e: 
        log(f"讀取錯誤: {e}")
    finally:
        socketio.emit('program_status', {'status': 'finished'})
        if fd:
            try: os.close(fd) 
            except: pass
        # [清理] 刪除暫存檔
        try:
            if src_file and os.path.exists(src_file): os.remove(src_file)
            if exe_file and os.path.exists(exe_file): os.remove(exe_file)
        except: pass

@socketio.on('send_input')
def handle_input(data):
    global master_fd_global
    if master_fd_global:
        try: 
            # 加上 \n 模擬按下 Enter
            msg = (data.get('input') + "\n").encode()
            os.write(master_fd_global, msg)
        except Exception as e: log(f"寫入失敗: {e}")

@socketio.on('stop_code')
def handle_stop():
    global current_process
    if current_process:
        try: 
            os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
            log("使用者強制停止程式")
        except: pass
        emit('program_output', {'data': "\n[程式已停止]"})

if __name__ == '__main__':
    # [雲端修正] 獲取環境變數中的 PORT，如果沒有則預設 5000
    port = int(os.environ.get("PORT", 5000))
    log(f"伺服器啟動中 (Port: {port})...")
    # host='0.0.0.0' 是必須的，這樣外部網路才能訪問 Docker 容器
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
