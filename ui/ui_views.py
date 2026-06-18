import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from collections import deque
import logging
import webbrowser
import os

from data_utils import DataHandler
from iec61960_standard import IEC61960Standard
from exceptions import HardwareError, ValidationError

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class BatteryAppUI:
    def __init__(self, root, controller):
        self.root = root
        self.controller = controller
        self.hw = controller.hw
        self.data = controller.data
        self.config = controller.config

        self.iec_standard = IEC61960Standard(self.config.battery.rated_capacity)
        self._profile_map = {}
        self._selected_profile_type = None
        self._selected_profile_id = None

        max_pts = controller.config.system.max_points
        self.buf_t = deque(maxlen=max_pts)
        self.buf_v = deque(maxlen=max_pts)
        self.buf_i = deque(maxlen=max_pts)
        self.buf_soc = deque(maxlen=max_pts)
        self.buf_rin = deque(maxlen=max_pts)
        self.buf_temp = deque(maxlen=max_pts)
        self._t_count = 0

        self._loading_states = {}
        self._progress_vars = {}
        self.summary_items = {}

        self.configure_styles()
        self.setup_ui_structure()
        self.refresh_ports(silent=True)
        self._update_connection_status()
        self._schedule_status_tick()

        logger.info("BatteryAppUI initialized")

    # =========================================================
    # Loading / progress helpers
    # =========================================================
    def set_loading_state(self, widget_key: str, loading: bool, text: str = None):
        if widget_key not in self._loading_states:
            self._loading_states[widget_key] = {"original_text": "", "loading": False}

        state = self._loading_states[widget_key]
        widget = getattr(self, widget_key, None)
        if not widget:
            return

        if loading and not state["loading"]:
            state["original_text"] = widget.cget("text")
            widget.config(text=text or "Loading...", state="disabled")
            state["loading"] = True
        elif not loading and state["loading"]:
            widget.config(text=state["original_text"], state="normal")
            state["loading"] = False

    def handle_safety_trigger(self, reason: str):
        self.set_loading_state("btn_start_monitor", False)
        self.set_loading_state("btn_start_profile", False)
        messagebox.showerror(
            "Safety Triggered",
            f"System safety triggered: {reason}\n\nAll operations stopped."
        )
        self.status_text.set("SAFETY TRIGGERED - System Stopped")
        self._update_connection_status()

    def handle_profile_completed(self, data):
        self.set_loading_state("btn_start_profile", False)
        success = data if isinstance(data, bool) else data.get("success", False)
        if "Run Status" in self.summary_items:
            self.summary_items["Run Status"].set("Completed" if success else "Stopped")
        if success:
            if isinstance(data, dict) and data.get("report"):
                self._show_test_report(data.get("report", ""))
            else:
                messagebox.showinfo("Profile Completed", "Battery profile test completed successfully!")
        else:
            err = data.get("error", "") if isinstance(data, dict) else ""
            messagebox.showwarning("Profile Stopped", err or "Battery profile test was stopped.")

    def _show_test_report(self, report: str):
        win = tk.Toplevel(self.root)
        win.title("IEC 61960 Test Report")
        win.geometry("700x500")
        text = tk.Text(win, wrap="word", font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        text.insert("1.0", report)
        text.config(state="disabled")

    # =========================================================
    # STYLES
    # =========================================================
    def configure_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        PRIMARY_COLOR = "#005a9e"
        SECONDARY_COLOR = "#5c5c5c"
        SUCCESS_COLOR = "#107c10"
        WARNING_COLOR = "#d83b01"
        DANGER_COLOR = "#c50f1f"
        BG_COLOR = "#F5F5F5"
        CARD_BG = "#E0E0E0"
        TEXT_DARK = "#1a1a1a"
        TEXT_LIGHT = "#4d4d4d"

        bold_font = ("Segoe UI", 10, "bold")
        title_font = ("Segoe UI", 11, "bold")
        main_font = ("Segoe UI", 10)
        num_font = ("Consolas", 22, "bold")

        style.configure(".", font=main_font, background=BG_COLOR, foreground=TEXT_DARK)
        style.configure("TFrame", background=BG_COLOR)
        style.configure("Sidebar.TFrame", background=CARD_BG)
        style.configure("Display.TFrame", background=BG_COLOR, relief="flat")
        style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"), foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("Title.TLabel", font=title_font, foreground=PRIMARY_COLOR, background=CARD_BG)
        style.configure("TLabel", background=CARD_BG, foreground=TEXT_DARK)
        style.map("TLabel", foreground=[("active", TEXT_DARK), ("disabled", TEXT_LIGHT)])
        style.configure("TRadiobutton", background=CARD_BG, foreground=TEXT_DARK)
        style.configure("TNotebook", background=CARD_BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=bold_font, padding=[12, 6], background="#d1d1d1", foreground=TEXT_DARK)
        style.map("TNotebook.Tab", background=[("selected", PRIMARY_COLOR)], foreground=[("selected", "white")])
        style.configure("Sidebar.TLabelframe", background=CARD_BG, font=bold_font, borderwidth=1, foreground=TEXT_DARK)
        style.configure("Sidebar.TLabelframe.Label", background=CARD_BG, foreground=TEXT_DARK, font=title_font)
        style.configure("TButton", background="#cccccc", foreground=TEXT_DARK)
        style.map("TButton", background=[("active", "#b3b3b3")], foreground=[("active", TEXT_DARK)])

        for name, color, pressed in [
            ("Primary", PRIMARY_COLOR, "#004578"),
            ("Success", SUCCESS_COLOR, "#0b5a0b"),
            ("Danger", DANGER_COLOR, "#990b16"),
        ]:
            style.configure(f"{name}.TButton", font=bold_font, foreground="white", background=color)
            style.map(
                f"{name}.TButton",
                background=[("pressed", pressed), ("active", pressed)],
                foreground=[("pressed", "white"), ("active", "white")],
            )

        style.configure("V.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("I.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("SoC.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("Rin.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("Temp.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("SoH.TLabel", font=num_font, foreground=TEXT_DARK, background=BG_COLOR)
        style.configure("MetricTitle.TLabel", font=("Segoe UI", 9, "bold"), foreground=TEXT_LIGHT, background=BG_COLOR)
        style.configure("MetricValue.TLabel", font=("Consolas", 18, "bold"), foreground=TEXT_DARK, background=BG_COLOR)

        self.COLORS = {
            "primary": PRIMARY_COLOR,
            "secondary": SECONDARY_COLOR,
            "success": SUCCESS_COLOR,
            "warning": WARNING_COLOR,
            "danger": DANGER_COLOR,
            "bg": BG_COLOR,
            "card_bg": CARD_BG,
            "text_dark": TEXT_DARK,
            "text_light": TEXT_LIGHT,
        }

    # =========================================================
    # LAYOUT
    # =========================================================
    def setup_ui_structure(self):
        self.root.title("ASET LABORATORY - Advanced Battery Characterization System v2.0")
        self.root.geometry("1600x950")
        self.root.configure(bg=self.COLORS["bg"])
        try:
            self.root.state("zoomed")
        except Exception:
            try:
                self.root.attributes("-zoomed", True)
            except Exception:
                pass

        self.create_application_menu()
        self.create_header()

        self.main_container = ttk.Frame(self.root, style="TFrame", padding=(10, 8))
        self.main_container.pack(fill=tk.BOTH, expand=True)

        self.sidebar = ttk.Frame(self.main_container, width=380, style="Sidebar.TFrame", padding=(8, 10))
        self.sidebar.pack(side="left", fill="y", padx=(0, 10), pady=2)
        self.sidebar.pack_propagate(False)

        self.content_area = ttk.Frame(self.main_container, style="Display.TFrame")
        self.content_area.pack(side="left", fill=tk.BOTH, expand=True)

        self.create_sidebar_controls()
        self.create_top_dashboard()
        self.create_summary_panel()

        self.content_display = ttk.Frame(self.content_area, style="Display.TFrame")
        self.content_display.pack(fill=tk.BOTH, expand=True)
        self.create_graphs(self.content_display)
        self.create_status_bar()

    def create_application_menu(self):
        try:
            menu_bar = tk.Menu(self.root, tearoff=0)

            file_menu = tk.Menu(menu_bar, tearoff=0)
            file_menu.add_command(label="Load Custom Profile CSV...", command=self.load_custom_profile)
            file_menu.add_command(label="Export Report", command=self.export_report)
            file_menu.add_separator()
            file_menu.add_command(label="Exit", command=self.root.quit)
            menu_bar.add_cascade(label="File", menu=file_menu)

            tools_menu = tk.Menu(menu_bar, tearoff=0)
            tools_menu.add_command(label="Refresh Ports", command=self.refresh_ports)
            tools_menu.add_command(label="Recalibrate OCV", command=self.calibrate_soc_from_ocv)
            tools_menu.add_command(label="Open Web Dashboard", command=self.open_web_dashboard)
            menu_bar.add_cascade(label="Tools", menu=tools_menu)

            settings_menu = tk.Menu(menu_bar, tearoff=0)
            settings_menu.add_command(label="Save Configuration", command=self.save_configuration)
            menu_bar.add_cascade(label="Settings", menu=settings_menu)

            help_menu = tk.Menu(menu_bar, tearoff=0)
            help_menu.add_command(
                label="About",
                command=lambda: messagebox.showinfo(
                    "About",
                    "ASET Battery System v2.0\n"
                    f"Battery: {self.config.battery.battery_type}\n"
                    f"Capacity: {self.config.battery.rated_capacity} Ah\n"
                    "IEC 61960 Ready",
                ),
            )
            menu_bar.add_cascade(label="Help", menu=help_menu)
            self.root.config(menu=menu_bar)
        except tk.TclError as e:
            logger.error(f"Failed to create application menu: {e}")

    def create_header(self):
        banner = tk.Frame(self.root, bg=self.COLORS["card_bg"], height=70)
        banner.pack(side="top", fill="x")
        banner.pack_propagate(False)

        title_frame = ttk.Frame(banner, style="TFrame")
        title_frame.pack(side="left", padx=(20, 0), pady=0, fill="y", expand=True)

        # Load and display logos
        if PIL_AVAILABLE:
            try:
                # Load the first logo (Faculty of Engineering UBU)
                path_eng_ubu_logo = os.path.join(os.path.dirname(__file__), "..", "ui", "00021f2021030914260622.png")
                img_eng_ubu = Image.open(path_eng_ubu_logo)
                img_eng_ubu = img_eng_ubu.resize((60, 60), Image.Resampling.LANCZOS)
                self.photo_eng_ubu = ImageTk.PhotoImage(img_eng_ubu)
                lbl_eng_ubu = ttk.Label(title_frame, image=self.photo_eng_ubu, background=self.COLORS["card_bg"])
                lbl_eng_ubu.grid(row=0, column=0, rowspan=2, padx=(0, 10), sticky="w")
            except Exception as e:
                logger.warning(f"Could not load logo_eng_ubu.png: {e}")
                self.photo_eng_ubu = None # Ensure it\'s None if loading fails

            # ASET LABORATORY title
            ttk.Label(title_frame, text="ASET LABORATORY", style="Header.TLabel", background=self.COLORS["card_bg"]).grid(row=0, column=1, sticky="w")
            ttk.Label(
                title_frame,
                text="Advanced Battery Characterization System",
                font=("Segoe UI", 10),
                foreground=self.COLORS["text_light"],
                background=self.COLORS["card_bg"],
            ).grid(row=1, column=1, sticky="w")

            try:
                # Load the second logo (EN UBU colorful logo)
                path_enubu_logo = os.path.join(os.path.dirname(__file__), "..", "ui", "00021b2021031713352962.png")
                img_enubu = Image.open(path_enubu_logo)
                img_enubu = img_enubu.resize((100, 60), Image.Resampling.LANCZOS) # Adjust size as needed
                self.photo_enubu = ImageTk.PhotoImage(img_enubu)
                lbl_enubu = ttk.Label(title_frame, image=self.photo_enubu, background=self.COLORS["card_bg"])
                lbl_enubu.grid(row=0, column=2, rowspan=2, padx=(10, 0), sticky="e")
            except Exception as e:
                logger.warning(f"Could not load logo_enubu.png: {e}")
                self.photo_enubu = None # Ensure it\'s None if loading fails



        mode = "SIMULATION" if self.config.system.simulation_mode else "HARDWARE"
        badge = tk.Label(
            banner,
            text=f"IEC 61960 | {mode}",
            bg=self.COLORS["primary"],
            fg="white",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=6,
        )
        badge.place(relx=0.98, rely=0.5, anchor="e")

    def create_sidebar_controls(self):
        ports_frame = ttk.Labelframe(self.sidebar, text="Connections", style="Sidebar.TLabelframe")
        ports_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(ports_frame, text="PSU (VISA):").pack(anchor="w")
        self.psu_port_cb = ttk.Combobox(ports_frame, state="readonly")
        self.psu_port_cb.pack(fill="x", pady=2)

        ttk.Label(ports_frame, text="Load (VISA):").pack(anchor="w")
        self.load_port_cb = ttk.Combobox(ports_frame, state="readonly")
        self.load_port_cb.pack(fill="x", pady=2)

        ttk.Label(ports_frame, text="ESP32 (COM):").pack(anchor="w")
        self.esp_port_cb = ttk.Combobox(ports_frame, state="readonly")
        self.esp_port_cb.pack(fill="x", pady=2)

        conn_btns = ttk.Frame(ports_frame)
        conn_btns.pack(fill="x", pady=(6, 0))
        self.btn_connect = ttk.Button(conn_btns, text="Connect", style="Success.TButton", command=self._connect_hardware)
        self.btn_connect.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_disconnect = ttk.Button(conn_btns, text="Disconnect", style="Danger.TButton", command=self._disconnect_hardware)
        self.btn_disconnect.pack(side="left", expand=True, fill="x", padx=(4, 0))
        ttk.Button(ports_frame, text="Refresh Ports", command=self.refresh_ports).pack(fill="x", pady=(6, 0))

        manual_frame = ttk.Labelframe(self.sidebar, text="Manual Control", style="Sidebar.TLabelframe")
        manual_frame.pack(fill="x", pady=(8, 8))

        psu_row = ttk.Frame(manual_frame)
        psu_row.pack(fill="x", pady=2)
        ttk.Label(psu_row, text="PSU V:").pack(side="left")
        self.psu_voltage_var = tk.StringVar(value="3.3")
        ttk.Entry(psu_row, textvariable=self.psu_voltage_var, width=8).pack(side="left", padx=4)
        ttk.Button(psu_row, text="ON", command=self._psu_on).pack(side="left", padx=2)
        ttk.Button(psu_row, text="OFF", command=self._psu_off).pack(side="left", padx=2)

        load_row = ttk.Frame(manual_frame)
        load_row.pack(fill="x", pady=2)
        ttk.Label(load_row, text="Load A:").pack(side="left")
        self.load_current_var = tk.StringVar(value="0.5")
        ttk.Entry(load_row, textvariable=self.load_current_var, width=8).pack(side="left", padx=4)
        ttk.Button(load_row, text="ON", command=self._load_on).pack(side="left", padx=2)
        ttk.Button(load_row, text="OFF", command=self._load_off).pack(side="left", padx=2)

        soc_row = ttk.Frame(manual_frame)
        soc_row.pack(fill="x", pady=(6, 2))
        ttk.Label(soc_row, text="Start SoC %:").pack(side="left")
        self.initial_soc_var = tk.StringVar(value="50")
        ttk.Entry(soc_row, textvariable=self.initial_soc_var, width=6).pack(side="left", padx=4)
        ttk.Button(soc_row, text="Sync", command=self._sync_initial_soc).pack(side="left")

        profile_frame = ttk.Labelframe(self.sidebar, text="Test Profiles", style="Sidebar.TLabelframe")
        profile_frame.pack(fill="x", pady=(8, 8))

        self.profile_listbox = tk.Listbox(
            profile_frame,
            height=8,
            bg="#1e293b",
            fg="#e5e7eb",
            selectbackground="#00b3a4",
            highlightthickness=0,
            borderwidth=0,
        )
        self.profile_listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self.profile_listbox.bind("<<ListboxSelect>>", self._on_profile_select)
        self._populate_iec_profiles()

        prof_btns = ttk.Frame(profile_frame)
        prof_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(prof_btns, text="Load CSV", command=self.load_custom_profile).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.btn_start_profile = ttk.Button(
            prof_btns, text="RUN", style="Primary.TButton", command=self._start_profile
        )
        self.btn_start_profile.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_stop_profile = ttk.Button(
            prof_btns, text="STOP", style="Danger.TButton", command=self._stop_profile
        )
        self.btn_stop_profile.pack(side="left", expand=True, fill="x", padx=(2, 0))

        ops_frame = ttk.Labelframe(self.sidebar, text="Operations", style="Sidebar.TLabelframe")
        ops_frame.pack(fill="x", pady=(8, 8))

        self.btn_start_monitor = ttk.Button(
            ops_frame, text="START MONITOR", style="Success.TButton", command=self._start_monitor
        )
        self.btn_start_monitor.pack(fill="x", pady=2)
        self.btn_stop_monitor = ttk.Button(
            ops_frame, text="STOP MONITOR", style="Danger.TButton", command=self._stop_monitor
        )
        self.btn_stop_monitor.pack(fill="x", pady=2)
        self.btn_calibrate = ttk.Button(
            ops_frame, text="CALIBRATE OCV", style="Primary.TButton", command=self.calibrate_soc_from_ocv
        )
        self.btn_calibrate.pack(fill="x", pady=2)
        self.btn_logging = ttk.Button(ops_frame, text="START DATA LOGGING", command=self._toggle_logging)
        self.btn_logging.pack(fill="x", pady=2)

    def create_top_dashboard(self):
        dash = ttk.Frame(self.content_area)
        dash.pack(fill="x", pady=(0, 8))

        metrics = [
            ("Voltage", "v_label", "V.TLabel", "0.000 V"),
            ("Current", "i_label", "I.TLabel", "0.000 A"),
            ("SoC", "soc_label", "SoC.TLabel", "0.0 %"),
            ("Rin", "rin_label", "Rin.TLabel", "0 mOhm"),
            ("Temp", "temp_label", "Temp.TLabel", "0.0 C"),
            ("SoH", "soh_label", "SoH.TLabel", "100.0 %"),
        ]
        for title, attr, style_name, default in metrics:
            card = ttk.Frame(dash, style="Display.TFrame", padding=8)
            card.pack(side="left", fill="x", expand=True, padx=4)
            ttk.Label(card, text=title, style="MetricTitle.TLabel").pack(anchor="w")
            lbl = ttk.Label(card, text=default, style=style_name)
            lbl.pack(anchor="w")
            setattr(self, attr, lbl)

    def create_summary_panel(self):
        summary = ttk.Labelframe(self.sidebar, text="Run Summary", style="Sidebar.TLabelframe")
        summary.pack(fill="x", pady=(8, 0))

        fields = [
            ("Connection", "Disconnected"),
            ("Monitor", "Stopped"),
            ("Logging", "Off"),
            ("Run Status", "Idle"),
            ("Profile", "None"),
            ("Battery", f"{self.config.battery.battery_type} {self.config.battery.rated_capacity}Ah"),
        ]
        for name, default in fields:
            row = ttk.Frame(summary)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=f"{name}:", width=12).pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Label(row, textvariable=var, foreground="#a3a3a3").pack(side="left")
            self.summary_items[name] = var

        self.lbl_profile_status = ttk.Label(summary, text="No profile loaded", foreground="#a3a3a3")
        self.lbl_profile_status.pack(anchor="w", pady=(6, 0))

    def create_graphs(self, parent):
        self.fig = Figure(figsize=(10, 6), dpi=90, facecolor=self.COLORS["bg"])
        self.axes = []
        titles = ["Voltage (V)", "Current (A)", "SoC (%)", "Rin (mOhm)", "Temperature (C)"]
        colors = ["#005a9e", "#d83b01", "#107c10", "#5c5c5c", "#8b0000"]

        for i, (title, color) in enumerate(zip(titles, colors)):
            ax = self.fig.add_subplot(3, 2, i + 1)
            ax.set_title(title, color=self.COLORS["text_dark"], fontsize=9)
            ax.set_facecolor("#ffffff")
            ax.tick_params(colors=self.COLORS["text_light"], labelsize=7)
            ax.grid(True, alpha=0.4, color="#d1d1d1")
            for spine in ax.spines.values():
                spine.set_color("#a3a3a3")
            line, = ax.plot([], [], color=color, linewidth=1.5)
            self.axes.append((ax, line))

        self.fig.tight_layout(pad=2.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def create_status_bar(self):
        status = ttk.Frame(self.content_area)
        status.pack(fill="x", side="bottom")
        self.status_text = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_text).pack(side="left", padx=8)
        ttk.Label(
            status,
            text=f"CSV: {self.config.system.csv_filepath}",
            foreground="#a3a3a3",
        ).pack(side="right", padx=8)

    # =========================================================
    # Display updates
    # =========================================================
    def update_display(self, v, i, soc, rin, temp=None, soh=None):
        if temp is None:
            temp = getattr(self.hw, "current_temp", 25.0)
        if soh is None:
            soh = getattr(self.controller.estimator, "soh", 100.0)

        self.v_label.config(text=f"{v:.3f} V")
        self.i_label.config(text=f"{i:.3f} A")
        self.soc_label.config(text=f"{soc:.1f} %")
        self.rin_label.config(text=f"{rin * 1000:.1f} mOhm")
        self.temp_label.config(text=f"{temp:.1f} C")
        self.soh_label.config(text=f"{soh:.1f} %")

        self.buf_t.append(self._t_count)
        self.buf_v.append(v)
        self.buf_i.append(i)
        self.buf_soc.append(soc)
        self.buf_rin.append(rin * 1000)
        self.buf_temp.append(temp)
        self._t_count += 1
        self._refresh_graphs()

        if self.controller.monitor_running:
            self.summary_items["Monitor"].set("Running")

    def _refresh_graphs(self):
        if not self.buf_t:
            return
        t = list(self.buf_t)
        series = [list(self.buf_v), list(self.buf_i), list(self.buf_soc), list(self.buf_rin), list(self.buf_temp)]
        for (ax, line), y in zip(self.axes, series):
            line.set_data(t, y)
            ax.relim()
            ax.autoscale_view()
        self.canvas.draw_idle()

    def _schedule_status_tick(self):
        self._update_connection_status()
        if self.data.is_recording:
            self.summary_items["Logging"].set("Recording")
            self.btn_logging.config(text="STOP DATA LOGGING")
        else:
            self.summary_items["Logging"].set("Off")
            self.btn_logging.config(text="START DATA LOGGING")
        self.root.after(1000, self._schedule_status_tick)

    def _update_connection_status(self):
        if getattr(self.hw, "is_connected", False):
            self.summary_items["Connection"].set("Connected")
            self.status_text.set("Hardware connected")
        else:
            self.summary_items["Connection"].set("Disconnected")
            if not self.controller.safety_triggered:
                self.status_text.set("Ready - connect hardware to begin")

    # =========================================================
    # Profile management
    # =========================================================
    def _populate_iec_profiles(self):
        self.profile_listbox.delete(0, tk.END)
        self._profile_map.clear()
        for test_id in self.iec_standard.get_available_tests():
            profile = self.iec_standard.get_test_profile(test_id)
            if not profile:
                continue
            display = f"[IEC] {profile.name}"
            self.profile_listbox.insert(tk.END, display)
            self._profile_map[display] = ("iec", test_id)

    def _on_profile_select(self, _event=None):
        try:
            display = self.profile_listbox.get(self.profile_listbox.curselection())
        except Exception:
            return
        self._selected_profile_type, self._selected_profile_id = self._profile_map.get(display, (None, None))
        self.lbl_profile_status.config(text=f"Selected: {display}")
        self.summary_items["Profile"].set(display[:30])

    def load_custom_profile(self):
        path = filedialog.askopenfilename(
            title="Load Current Profile CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        data, err = DataHandler.load_profile_csv(path, default_dt=1.0)
        if err:
            messagebox.showerror("Profile Error", err)
            return
        if not data:
            messagebox.showwarning("Profile Error", "No valid profile steps found in CSV.")
            return

        self.controller.profile_data = data
        display = f"[CSV] {path.split('/')[-1].split(chr(92))[-1]}"
        self.profile_listbox.insert(tk.END, display)
        self._profile_map[display] = ("csv", path)
        self.profile_listbox.selection_clear(0, tk.END)
        self.profile_listbox.selection_set(tk.END)
        self._on_profile_select()
        self.lbl_profile_status.config(text=f"Loaded {len(data)} steps from CSV")
        messagebox.showinfo("Profile Loaded", f"Loaded {len(data)} steps from:\n{path}")

    # =========================================================
    # Hardware / controller actions
    # =========================================================
    def refresh_ports(self, silent=False):
        try:
            visa_ports = self.hw.get_visa_ports() if hasattr(self.hw, "get_visa_ports") else []
            com_ports = self.hw.get_com_ports() if hasattr(self.hw, "get_com_ports") else []
            self.psu_port_cb["values"] = visa_ports
            self.load_port_cb["values"] = visa_ports
            self.esp_port_cb["values"] = com_ports

            hw_cfg = self.config.hardware
            if hw_cfg.psu_port and hw_cfg.psu_port in visa_ports:
                self.psu_port_cb.set(hw_cfg.psu_port)
            elif visa_ports:
                self.psu_port_cb.set(visa_ports[0])
            if hw_cfg.load_port and hw_cfg.load_port in visa_ports:
                self.load_port_cb.set(hw_cfg.load_port)
            elif len(visa_ports) > 1:
                self.load_port_cb.set(visa_ports[1])
            elif visa_ports:
                self.load_port_cb.set(visa_ports[0])
            if hw_cfg.esp_port and hw_cfg.esp_port in com_ports:
                self.esp_port_cb.set(hw_cfg.esp_port)
            elif com_ports:
                self.esp_port_cb.set(com_ports[0])

            if not silent:
                messagebox.showinfo(
                    "Refresh Ports",
                    f"VISA: {len(visa_ports)} | COM: {len(com_ports)}",
                )
        except Exception as e:
            logger.error(f"Error refreshing ports: {e}")
            if not silent:
                messagebox.showerror("Refresh Ports", str(e))

    def _connect_hardware(self):
        psu = self.psu_port_cb.get().strip()
        load = self.load_port_cb.get().strip()
        esp = self.esp_port_cb.get().strip()
        if not psu or not load:
            messagebox.showwarning("Connect", "Please select PSU and Load ports.")
            return
        try:
            self.set_loading_state("btn_connect", True, "Connecting...")
            self.hw.connect_instruments(psu, load)
            if esp:
                self.hw.connect_esp32(esp)
            self.config.hardware.psu_port = psu
            self.config.hardware.load_port = load
            self.config.hardware.esp_port = esp
            self.config.save_config()
            self._update_connection_status()
            messagebox.showinfo("Connect", "Hardware connected successfully.")
        except Exception as e:
            messagebox.showerror("Connect Error", str(e))
        finally:
            self.set_loading_state("btn_connect", False)

    def _disconnect_hardware(self):
        try:
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            elif hasattr(self.hw, "shutdown_all"):
                self.hw.shutdown_all()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            self._update_connection_status()
            messagebox.showinfo("Disconnect", "Hardware disconnected.")
        except Exception as e:
            messagebox.showerror("Disconnect Error", str(e))

    def _psu_on(self):
        try:
            v = float(self.psu_voltage_var.get())
            self.hw.set_psu(True, str(v))
        except ValueError:
            messagebox.showerror("PSU", "Invalid voltage value.")

    def _psu_off(self):
        self.hw.set_psu(False)

    def _load_on(self):
        try:
            a = float(self.load_current_var.get())
            self.hw.set_load(True, str(a))
        except ValueError:
            messagebox.showerror("Load", "Invalid current value.")

    def _load_off(self):
        self.hw.set_load(False)

    def _sync_initial_soc(self):
        try:
            soc = float(self.initial_soc_var.get())
            if not 0 <= soc <= 100:
                raise ValidationError("SoC must be between 0 and 100")
            self.controller.estimator.set_initial_soc(soc)
            messagebox.showinfo("SoC Sync", f"Initial SoC set to {soc:.1f}%")
        except (ValueError, ValidationError) as e:
            messagebox.showerror("SoC Sync", str(e))

    def _start_monitor(self):
        if not self.hw.is_connected:
            messagebox.showwarning("Monitor", "Connect hardware first.")
            return
        try:
            self.set_loading_state("btn_start_monitor", True, "Starting...")
            self.controller.start_monitor()
            self.summary_items["Monitor"].set("Running")
            self.status_text.set("Monitor running")
        except Exception as e:
            messagebox.showerror("Monitor Error", str(e))
        finally:
            self.set_loading_state("btn_start_monitor", False)

    def _stop_monitor(self):
        try:
            self.controller.stop_monitor()
            self.summary_items["Monitor"].set("Stopped")
            self.status_text.set("Monitor stopped")
        except Exception as e:
            messagebox.showerror("Monitor Error", str(e))

    def _start_profile(self):
        if not self.hw.is_connected:
            messagebox.showwarning("Profile", "Connect hardware first.")
            return
        try:
            display = self.profile_listbox.get(self.profile_listbox.curselection())
        except Exception:
            messagebox.showwarning("Profile", "Please select a profile first.")
            return

        ptype, pid = self._profile_map.get(display, (None, None))
        try:
            self.set_loading_state("btn_start_profile", True, "RUNNING...")
            self.summary_items["Run Status"].set("Running")
            if ptype == "iec":
                self.controller.start_iec61960_test(pid, self.iec_standard)
            elif ptype == "csv":
                self.controller.start_profile()
            else:
                messagebox.showwarning("Profile", "Unknown profile type.")
        except Exception as e:
            messagebox.showerror("Profile Error", str(e))
            self.set_loading_state("btn_start_profile", False)

    def _stop_profile(self):
        self.controller.stop_profile()
        self.summary_items["Run Status"].set("Stopped")

    def _toggle_logging(self):
        if self.data.is_recording:
            self.data.stop_logging()
            self.summary_items["Logging"].set("Off")
            self.btn_logging.config(text="START DATA LOGGING")
        else:
            ok, msg = self.data.start_logging(self.config.system.csv_filepath)
            if ok:
                self.summary_items["Logging"].set("Recording")
                self.btn_logging.config(text="STOP DATA LOGGING")
            else:
                messagebox.showerror("Logging Error", msg)

    def calibrate_soc_from_ocv(self):
        try:
            soc = self.controller.calibrate_from_ocv()
            messagebox.showinfo("Calibration Complete", f"SoC calibrated to {soc:.1f}% from OCV")
            self.update_display(
                *self._read_current_measurements(),
                soc,
                self.controller.estimator.rin,
            )
        except HardwareError as e:
            messagebox.showerror("Calibration Error", str(e))
        except Exception as e:
            logger.error(f"Calibration error: {e}")
            messagebox.showerror("Calibration Error", str(e))

    def _read_current_measurements(self):
        try:
            v, psu_i, load_i = self.hw.read_vi()
            return v, psu_i - load_i
        except Exception:
            return 0.0, 0.0

    def save_configuration(self):
        if self.config.save_config():
            messagebox.showinfo("Settings", "Configuration saved.")
        else:
            messagebox.showerror("Settings", "Failed to save configuration.")

    def export_report(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        state = self.controller.estimator.get_state()
        report = (
            "ASET Battery Test Report\n"
            "========================\n"
            f"Battery Type: {self.config.battery.battery_type}\n"
            f"Rated Capacity: {self.config.battery.rated_capacity} Ah\n"
            f"SoC: {state['soc']:.2f}%\n"
            f"SoH: {state['soh']:.2f}%\n"
            f"Rin: {state['rin'] * 1000:.2f} mOhm\n"
            f"AH Accumulated: {state['ah_accumulated']:.4f}\n"
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            messagebox.showinfo("Export", f"Report saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def open_web_dashboard(self):
        port = getattr(self.config.system, "web_server_port", 8000)
        webbrowser.open(f"http://127.0.0.1:{port}/")
