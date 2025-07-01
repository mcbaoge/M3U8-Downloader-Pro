import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
import threading
import os
import re
import queue
import subprocess
from urllib.parse import urljoin
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed


class DownloadManager:
    def __init__(self, url, save_path, progress_callback):
        self.m3u8_url = url
        self.save_path = save_path
        self.progress_callback = progress_callback
        self.ts_urls = []
        self.failed_segments = []
        self.completed = 0
        self.total = 0
        self.stop_requested = False
        self.executor = None

        # 确保保存路径存在
        os.makedirs(save_path, exist_ok=True)
        self.temp_dir = os.path.join(save_path, "temp_ts_files")
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.makedirs(self.temp_dir, exist_ok=True)

        # 设置请求头
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': self.m3u8_url
        }

    def parse_m3u8(self):
        try:
            resp = requests.get(self.m3u8_url, headers=self.headers, timeout=10)
            resp.raise_for_status()

            content = resp.text
            if not content:
                raise Exception("Empty M3U8 file")

            # 处理特殊格式的m3u8文件
            content = re.sub(r'#EXT-X-BYTERANGE.*\n?', '', content)
            lines = [line.strip() for line in content.splitlines() if line.strip()]

            # 获取真正的ts文件链接
            base_url = os.path.dirname(self.m3u8_url) + '/'
            self.ts_urls = [urljoin(base_url, line) for line in lines if not line.startswith('#')]

            if not self.ts_urls:
                raise Exception("No TS files found in M3U8")

            self.total = len(self.ts_urls)
            return True

        except Exception as e:
            self.progress_callback(0, 0, error=f"解析M3U8文件失败: {str(e)}")
            return False

    def download_segment(self, idx, url, max_retries=3):
        if self.stop_requested:
            return idx, "下载已取消", False

        ts_path = os.path.join(self.temp_dir, f"segment_{idx:05d}.ts")

        for retry in range(max_retries):
            if self.stop_requested:
                return idx, "下载已取消", False

            try:
                response = requests.get(url, headers=self.headers, stream=True, timeout=30)
                response.raise_for_status()

                with open(ts_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:  # 过滤掉 keep-alive 的 chunk
                            f.write(chunk)
                return idx, None, True

            except Exception as e:
                if retry < max_retries - 1:
                    continue
                return idx, f"下载失败: {str(e)}", False

        return idx, "未知错误", False

    def start_download(self, max_workers=10):
        self.stop_requested = False
        self.completed = 0
        self.failed_segments = []

        if not self.parse_m3u8():
            return False

        # 使用线程池并发下载
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            self.executor = executor
            futures = {executor.submit(self.download_segment, idx, url): idx for idx, url in enumerate(self.ts_urls)}

            for future in as_completed(futures):
                idx, error, success = future.result()
                self.completed += 1

                if self.stop_requested:
                    self.progress_callback(self.completed, self.total, "下载已取消")
                    return False

                if not success:
                    self.failed_segments.append((idx, error))
                    self.progress_callback(self.completed, self.total,
                                           error=f"失败片段: {len(self.failed_segments)}")

                progress = int(self.completed / self.total * 100)
                self.progress_callback(self.completed, self.total,
                                       f"下载中: {self.completed}/{self.total} ({progress}%)")

        if self.failed_segments:
            self.progress_callback(self.completed, self.total,
                                   error=f"部分片段下载失败 ({len(self.failed_segments)}个)")
            return False

        return True

    def stop_download(self):
        self.stop_requested = True
        if self.executor:
            self.executor.shutdown(wait=False)

    def merge_files(self):
        try:
            # 获取所有下载的分片文件
            ts_files = sorted([
                os.path.join(self.temp_dir, f)
                for f in os.listdir(self.temp_dir)
                if f.endswith('.ts')
            ])

            if not ts_files:
                raise Exception("未找到分片文件")

            # 创建合并列表文件
            list_file = os.path.join(self.temp_dir, "file_list.txt")
            with open(list_file, 'w') as f:
                for ts in ts_files:
                    f.write(f"file '{ts}'\n")

            # 使用FFmpeg无损转换并合并文件
            output_file = os.path.join(self.save_path, "output.mp4")

            if shutil.which("ffmpeg"):
                # 如果系统安装了ffmpeg，使用它来合并
                cmd = [
                    'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                    '-i', list_file, '-c', 'copy', output_file
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                return True
            else:
                # 如果ffmpeg不可用，则手动合并
                self.progress_callback(0, 0, "未找到FFmpeg，进行手动合并...")
                with open(output_file, 'wb') as outfile:
                    for ts_file in ts_files:
                        with open(ts_file, 'rb') as infile:
                            shutil.copyfileobj(infile, outfile)
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                return True

        except Exception as e:
            self.progress_callback(0, 0, error=f"合并文件失败: {str(e)}")
            return False


class M3U8DownloaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("高级 M3U8 视频下载器 (多线程)")
        self.root.geometry("750x600")
        self.root.resizable(True, True)

        # 设置应用图标
        try:
            self.root.iconbitmap("icon.ico")  # 请提供图标文件
        except:
            pass

        # 下载管理器
        self.download_manager = None

        # 设置主题和样式
        self.setup_styles()
        self.create_widgets()

    def setup_styles(self):
        # 创建样式对象
        self.style = ttk.Style()

        # 设置主题
        self.style.theme_use("clam")

        # 自定义进度条样式
        self.style.configure("Custom.Horizontal.TProgressbar",
                             thickness=15,
                             troughcolor="#f0f0f0",
                             bordercolor="#3a7bf6",
                             lightcolor="#3a7bf6",
                             darkcolor="#3a7bf6")

        # 设置字体
        self.title_font = ("Segoe UI", 14, "bold")
        self.label_font = ("Segoe UI", 10)
        self.button_font = ("Segoe UI", 10, "bold")
        self.info_font = ("Segoe UI", 9)
        self.log_font = ("Consolas", 9)

        # 定义颜色
        self.primary_color = "#3a7bf6"
        self.error_color = "#e74c3c"
        self.warning_color = "#f39c12"
        self.success_color = "#2ecc71"
        self.dark_bg = "#2c3e50"
        self.light_bg = "#f8f9fa"

    def apply_hover_effect(self, widget, bg_color, hover_color):
        # 应用鼠标悬停效果
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_color))
        widget.bind("<Leave>", lambda e: widget.config(bg=bg_color))

    def create_widgets(self):
        # 创建主框架
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        # 标题区
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 15))

        title_icon = tk.Label(header_frame, text="📥", font=("Segoe UI", 20))
        title_icon.pack(side=tk.LEFT, padx=(0, 10))

        title_label = ttk.Label(header_frame, text="M3U8 视频下载器", font=self.title_font)
        title_label.pack(side=tk.LEFT)

        subtitle_label = ttk.Label(header_frame, text="多线程版", font=("Segoe UI", 10))
        subtitle_label.pack(side=tk.LEFT, padx=10)

        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 配置部分框架
        config_frame = ttk.LabelFrame(main_frame, text=" 下载配置 ", padding=10)
        config_frame.pack(fill=tk.X, pady=(0, 15))

        # URL输入部分
        url_frame = ttk.Frame(config_frame)
        url_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(url_frame, text="M3U8 链接:", font=self.label_font,
                  width=12, anchor=tk.W).pack(side=tk.LEFT)
        self.url_entry = ttk.Entry(url_frame, font=self.label_font)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 5))

        # 保存路径部分
        path_frame = ttk.Frame(config_frame)
        path_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(path_frame, text="保存路径:", font=self.label_font,
                  width=12, anchor=tk.W).pack(side=tk.LEFT)
        self.path_entry = ttk.Entry(path_frame, font=self.label_font)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 5))
        self.browse_btn = tk.Button(path_frame, text="浏览...", font=self.button_font,
                                    command=self.choose_path, width=8, bg="#f0f0f0",
                                    relief="flat", bd=1, highlightthickness=0)
        self.browse_btn.pack(side=tk.LEFT, padx=(5, 0))
        self.apply_hover_effect(self.browse_btn, "#f0f0f0", "#e0e0e0")

        # 线程设置部分
        thread_frame = ttk.Frame(config_frame)
        thread_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(thread_frame, text="同时下载数:", font=self.label_font,
                  width=12, anchor=tk.W).pack(side=tk.LEFT)
        self.thread_var = tk.StringVar(value="10")
        thread_spin = ttk.Spinbox(thread_frame, from_=1, to=50, width=5,
                                  textvariable=self.thread_var, font=self.button_font)
        thread_spin.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(thread_frame, text="(推荐 5-20)", font=self.info_font, foreground="#777777").pack(side=tk.LEFT,
                                                                                                    padx=5)

        # 分隔线
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # 进度条部分
        progress_frame = ttk.LabelFrame(main_frame, text=" 下载进度 ", padding=10)
        progress_frame.pack(fill=tk.X, pady=(0, 15))

        self.progress = ttk.Progressbar(progress_frame, length=650,
                                        mode="determinate",
                                        style="Custom.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=5)

        # 状态标签
        status_frame = ttk.Frame(progress_frame)
        status_frame.pack(fill=tk.X, pady=(5, 0))

        self.status_label = ttk.Label(status_frame, text="准备下载...", font=self.label_font,
                                      anchor=tk.W, foreground="#333333")
        self.status_label.pack(fill=tk.X)

        # 下载信息
        info_frame = ttk.Frame(progress_frame)
        info_frame.pack(fill=tk.X, pady=(10, 0))

        info_card = ttk.Frame(info_frame, relief=tk.SOLID, borderwidth=1)
        info_card.pack(fill=tk.X, pady=5, padx=10, ipadx=5, ipady=5)

        ttk.Label(info_card, text="完成分片:", font=self.label_font, anchor=tk.W).grid(row=0, column=0, padx=10, pady=5,
                                                                                       sticky=tk.W)
        self.completed_label = ttk.Label(info_card, text="0/0", font=self.label_font, foreground=self.primary_color)
        self.completed_label.grid(row=0, column=1, padx=10, pady=5, sticky=tk.W)

        ttk.Label(info_card, text="失败数量:", font=self.label_font, anchor=tk.W).grid(row=0, column=2, padx=(30, 10),
                                                                                       pady=5, sticky=tk.W)
        self.failed_label = ttk.Label(info_card, text="0", font=self.label_font, foreground=self.error_color)
        self.failed_label.grid(row=0, column=3, padx=10, pady=5, sticky=tk.W)

        progress_info = ttk.Frame(info_card)
        progress_info.grid(row=0, column=4, padx=(30, 10), pady=5, sticky=tk.E)

        self.progress_label = ttk.Label(progress_info, text="0%", font=self.label_font, foreground="#555555")
        self.progress_label.pack(side=tk.RIGHT, padx=(0, 10))

        # 控制按钮
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(0, 15))

        self.download_btn = tk.Button(button_frame, text="开始下载", font=self.button_font,
                                      command=self.start_download, width=12,
                                      bg=self.primary_color, fg="white",
                                      relief="flat", bd=0, highlightthickness=0)
        self.download_btn.pack(side=tk.LEFT, padx=5)
        self.apply_hover_effect(self.download_btn, self.primary_color, "#2a68e0")

        self.cancel_btn = tk.Button(button_frame, text="取消下载", font=self.button_font,
                                    command=self.cancel_download, width=12,
                                    bg="#95a5a6", fg="white",
                                    relief="flat", bd=0, highlightthickness=0,
                                    state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)
        self.apply_hover_effect(self.cancel_btn, "#95a5a6", "#7f8c8d")

        self.merge_btn = tk.Button(button_frame, text="合并文件", font=self.button_font,
                                   command=self.merge_files, width=12,
                                   bg="#95a5a6", fg="white",
                                   relief="flat", bd=0, highlightthickness=0,
                                   state=tk.DISABLED)
        self.merge_btn.pack(side=tk.LEFT, padx=5)
        self.apply_hover_effect(self.merge_btn, "#95a5a6", "#7f8c8d")

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text=" 操作日志 ", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, font=self.log_font,
                                                  bg=self.light_bg, fg="#333333",
                                                  padx=10, pady=5)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

        # 状态栏
        footer_frame = ttk.Frame(self.root)
        footer_frame.pack(fill=tk.X, padx=10, pady=5)

        self.status_bar = ttk.Label(footer_frame, text="就绪", relief=tk.SUNKEN,
                                    anchor=tk.W, font=self.info_font,
                                    background="#f0f0f0", foreground="#555555")
        self.status_bar.pack(fill=tk.X)

    def choose_path(self):
        path = filedialog.askdirectory()
        if path:
            self.path_entry.delete(0, tk.END)
            self.path_entry.insert(0, path)

    def start_download(self):
        url = self.url_entry.get().strip()
        path = self.path_entry.get().strip()

        if not url:
            self.log_message("错误: 请输入M3U8链接", "error")
            self.status_bar.config(text="错误: 请输入M3U8链接", foreground=self.error_color)
            return
        if not path:
            self.log_message("错误: 请选择保存路径", "error")
            self.status_bar.config(text="错误: 请选择保存路径", foreground=self.error_color)
            return
        if not os.path.isdir(path):
            self.log_message("错误: 保存路径不存在", "error")
            self.status_bar.config(text="错误: 保存路径不存在", foreground=self.error_color)
            return

        try:
            max_workers = min(max(int(self.thread_var.get()), 1), 50)
        except:
            max_workers = 10

        # 初始化下载管理器
        self.download_manager = DownloadManager(url, path, self.update_progress)

        # 启用取消按钮，禁用下载按钮
        self.download_btn.config(state=tk.DISABLED, bg="#95a5a6")
        self.browse_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL, bg=self.error_color)
        self.merge_btn.config(state=tk.DISABLED, bg="#95a5a6")

        # 重置进度
        self.progress["value"] = 0
        self.completed_label.config(text="0/0")
        self.failed_label.config(text="0")
        self.progress_label.config(text="0%")
        self.status_label.config(text="正在下载...")
        self.status_bar.config(text="正在启动下载任务...", foreground="#333333")

        # 启动下载线程
        threading.Thread(target=self.download_thread, args=(max_workers,), daemon=True).start()
        self.log_message(f"开始下载: {url}", "info")
        self.log_message(f"保存到: {path}", "info")
        self.log_message(f"使用 {max_workers} 个线程同时下载", "info")
        self.status_bar.config(text=f"启动下载任务，使用 {max_workers} 个线程...", foreground="#333333")

    def download_thread(self, max_workers):
        try:
            success = self.download_manager.start_download(max_workers)
            if success:
                self.root.after(0, lambda: self.download_success())
            else:
                self.root.after(0, lambda: self.download_failed())
        except Exception as e:
            self.root.after(0, lambda: self.update_progress(0, 0, error=f"下载线程错误: {str(e)}"))
            self.root.after(0, lambda: self.download_failed())

    def download_success(self):
        self.log_message("下载完成，所有分片已成功下载", "success")
        self.status_label.config(text="下载完成，所有分片已成功下载", foreground=self.success_color)
        self.completed_label.config(text=f"{self.download_manager.completed}/{self.download_manager.total}")
        self.status_bar.config(text="下载完成，所有分片已成功下载", foreground=self.success_color)

        # 启用合并按钮
        self.merge_btn.config(state=tk.NORMAL, bg=self.success_color)
        self.download_btn.config(state=tk.NORMAL, bg=self.primary_color)
        self.browse_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED, bg="#95a5a6")

    def download_failed(self):
        if self.download_manager.stop_requested:
            self.log_message("下载已取消", "warning")
            self.status_label.config(text="下载已取消", foreground=self.warning_color)
            self.status_bar.config(text="下载已取消", foreground=self.warning_color)
        elif self.download_manager.failed_segments:
            self.log_message(f"下载部分失败，{len(self.download_manager.failed_segments)}个分片下载失败", "warning")
            self.status_label.config(text=f"下载部分失败，{len(self.download_manager.failed_segments)}个分片下载失败",
                                     foreground=self.warning_color)
            self.status_bar.config(text=f"下载部分失败，{len(self.download_manager.failed_segments)}个分片下载失败",
                                   foreground=self.warning_color)
        else:
            self.log_message("下载失败", "error")
            self.status_label.config(text="下载失败", foreground=self.error_color)
            self.status_bar.config(text="下载失败", foreground=self.error_color)

        self.completed_label.config(text=f"{self.download_manager.completed}/{self.download_manager.total}")
        self.failed_label.config(text=str(len(self.download_manager.failed_segments)))

        # 允许用户尝试合并文件
        self.merge_btn.config(state=tk.NORMAL, bg=self.success_color)
        self.download_btn.config(state=tk.NORMAL, bg=self.primary_color)
        self.browse_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED, bg="#95a5a6")

    def cancel_download(self):
        if self.download_manager:
            self.download_manager.stop_download()
            self.cancel_btn.config(state=tk.DISABLED, bg="#95a5a6")
            self.status_label.config(text="正在取消下载...", foreground=self.warning_color)
            self.status_bar.config(text="正在取消下载...", foreground=self.warning_color)
            self.log_message("正在取消下载...", "warning")

    def merge_files(self):
        if not self.download_manager:
            self.log_message("错误: 无下载任务", "error")
            self.status_bar.config(text="错误: 无下载任务", foreground=self.error_color)
            return

        # 禁用按钮
        self.merge_btn.config(state=tk.DISABLED, bg="#95a5a6")
        self.status_label.config(text="正在合并文件...", foreground="#9b59b6")
        self.status_bar.config(text="正在合并文件...", foreground="#9b59b6")
        self.log_message("开始合并文件...", "info")

        # 启动合并线程
        threading.Thread(target=self.merge_thread, daemon=True).start()

    def merge_thread(self):
        try:
            success = self.download_manager.merge_files()
            if success:
                self.root.after(0, lambda: self.merge_success())
            else:
                self.root.after(0, lambda: self.merge_failed())
        except Exception as e:
            self.root.after(0, lambda: self.log_message(f"合并线程错误: {str(e)}", "error"))
            self.root.after(0, lambda: self.merge_failed())

    def merge_success(self):
        self.log_message("文件合并成功！已保存为output.mp4", "success")
        self.status_label.config(text="文件合并成功！已保存为output.mp4", foreground=self.success_color)
        self.status_bar.config(text="文件合并成功！已保存为output.mp4", foreground=self.success_color)

        # 重置按钮状态
        self.merge_btn.config(state=tk.DISABLED, bg="#95a5a6")

    def merge_failed(self):
        self.log_message("文件合并失败", "error")
        self.status_label.config(text="文件合并失败", foreground=self.error_color)
        self.status_bar.config(text="文件合并失败", foreground=self.error_color)

        # 允许再次尝试合并
        self.merge_btn.config(state=tk.NORMAL, bg=self.success_color)

    def update_progress(self, current, total, message=None, error=None):
        # 确保进度条不超过100%
        if total > 0:
            progress = min(100, int(current / total * 100))
            self.progress["value"] = progress
            self.progress_label.config(text=f"{progress}%")
        else:
            progress = 0
            self.progress["value"] = 0
            self.progress_label.config(text="0%")

        # 更新状态标签
        if error:
            self.status_label.config(text=error, foreground=self.error_color)
        elif message:
            self.status_label.config(text=message, foreground="#333333")
        elif total > 0:
            self.status_label.config(text=f"下载进度: {current}/{total} ({progress}%)", foreground="#333333")
        else:
            self.status_label.config(text="准备下载...", foreground="#333333")

        # 更新数量标签
        if self.download_manager:
            self.completed_label.config(text=f"{self.download_manager.completed}/{self.download_manager.total}")
            self.failed_label.config(text=str(len(self.download_manager.failed_segments)))

    def log_message(self, message, msg_type="info"):
        self.log_text.config(state=tk.NORMAL)

        if msg_type == "error":
            tag = "error"
            color = self.error_color
        elif msg_type == "warning":
            tag = "warning"
            color = self.warning_color
        elif msg_type == "success":
            tag = "success"
            color = self.success_color
        else:
            tag = "info"
            color = self.primary_color

        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.tag_configure(tag, foreground=color)

        # 滚动到底部
        self.log_text.yview(tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.status_bar.config(text=f"日志: {message}"[:50], foreground=color)


if __name__ == "__main__":
    root = tk.Tk()
    app = M3U8DownloaderGUI(root)
    root.mainloop()