#!/usr/bin/env python3
"""
HMI - Máquina de taladrado y corte
PLC: Siemens S7-1200 CPU 1214C AC/DC/RLY
Raspberry Pi + pantalla táctil (Tkinter + python-snap7)

Dos pantallas:
  AUTOMÁTICO -> Marcha / Paro, pilotos y conteo de piezas
  MANUAL     -> Taladro, Sierra, V Cilindro, V Taladro, V Sierra y Banda
                (un toque enciende, otro toque apaga; máximo 3 a la vez)

MAPA DE MEMORIA (hay que usar las mismas marcas en TIA Portal)
  MB10 -> lo ESCRIBE la Raspberry (pulsos)
     M10.0  HMI_Marcha
     M10.1  HMI_Paro
     M10.2  HMI_Reset_Conteo
     M10.3  HMI_Manual_ON      (pedir entrar a modo manual)
     M10.4  HMI_Manual_OFF     (salir de modo manual)
  MB11 -> lo ESCRIBE el PLC
     M11.0  HMI_Maquina_ON
     M11.1  HMI_Taladro
     M11.2  HMI_Sierra
     M11.3  HMI_Emergencia
     M11.4  HMI_Modo_Manual    (el PLC aceptó el modo manual)
  MB12 -> lo ESCRIBE la Raspberry (estado, se reescribe cada 0.1 s)
     M12.0  HMI_Man_Taladro
     M12.1  HMI_Man_Sierra
     M12.2  HMI_Man_VA
     M12.3  HMI_Man_VB
     M12.4  HMI_Man_VC
     M12.5  HMI_Man_Banda
     M12.7  HMI_Vivo           (latido: si deja de llegar, el PLC corta manual)
  MD14 -> lo ESCRIBE el PLC: conteo de piezas (DInt)
  QB0/QB1 -> salidas reales (solo lectura):
     Q0.1 Banda, Q0.5 VB, Q0.6 Sierra, Q0.7 Taladro, Q1.0 VA, Q1.1 VC

SIMULADO = True  -> funciona sin PLC (para probar en casa)
SIMULADO = False -> se conecta al PLC real por Ethernet

Salir: tecla Esc, o mantener presionado el título 3 segundos.
"""

import threading
import time
import tkinter as tk

# ===================== CONFIGURACIÓN =====================
SIMULADO = False             # True = modo simulado (sin PLC)
PLC_IP = "192.168.0.1"       # IP de la CPU 1214C
RACK, SLOT = 0, 1            # S7-1200 siempre es rack 0, slot 1

BYTE_CMD = 10                # MB10: pulsos desde la HMI
BYTE_EST = 11                # MB11: estados desde el PLC
BYTE_MAN = 12                # MB12: botones manuales + latido
BYTE_CONTEO = 14             # MD14: conteo de piezas (DInt)

CMD_MARCHA = 0               # M10.0
CMD_PARO = 1                 # M10.1
CMD_RESET = 2                # M10.2
CMD_MAN_ON = 3               # M10.3
CMD_MAN_OFF = 4              # M10.4

EST_MAQUINA = 0              # M11.0
EST_TALADRO = 1              # M11.1
EST_SIERRA = 2               # M11.2
EST_EMERG = 3                # M11.3
EST_MANUAL = 4               # M11.4

BIT_VIVO = 7                 # M12.7

# Botones manuales: (clave, texto, bit en MB12, (byte Q, bit Q))
MANUALES = [
    ("taladro", "TALADRO",    0, (0, 7)),   # M12.0 -> Q0.7
    ("sierra",  "SIERRA",     1, (0, 6)),   # M12.1 -> Q0.6
    ("va",      "V CILINDRO", 2, (1, 0)),   # M12.2 -> Q1.0 (válvula A)
    ("vb",      "V TALADRO",  3, (0, 5)),   # M12.3 -> Q0.5 (válvula B)
    ("vc",      "V SIERRA",   4, (1, 1)),   # M12.4 -> Q1.1 (válvula C)
    ("banda",   "BANDA",      5, (0, 1)),   # M12.5 -> Q0.1
]
Q_BANDA = (0, 1)             # Q0.1 motor de la banda
MAX_MANUAL = 3               # máximo de salidas manuales encendidas a la vez

PERIODO = 0.1                # s entre ciclos de comunicación
PANTALLA_COMPLETA = True
OCULTAR_CURSOR = False       # True para que no se vea la flecha en la táctil

# Colores
FONDO = "#1e1f24"
PANEL = "#2a2c33"
TEXTO = "#e8e8e8"
GRIS = "#4a4d57"
GRIS_OSC = "#34363e"
VERDE = "#22a55a"
VERDE_ON = "#2fd16f"
ROJO = "#c0392b"
ROJO_ON = "#ff4b3a"
AMARILLO = "#f2b705"
AZUL = "#2f80ed"


def bit(valor, n):
    return (valor >> n) & 1 == 1


# ===================== PLC SIMULADO =====================
class PLCSimulado:
    """Imita la lógica del PLC para probar sin máquina."""

    def __init__(self):
        self.on = False
        self.emergencia = False
        self.manual = False
        self.man_bits = 0
        self.conteo = 0
        self.t_pieza = None

    def conectar(self):
        return True

    def desconectar(self):
        pass

    def escribir_pulsos(self, valor):
        if not valor:
            return
        if bit(valor, CMD_PARO):
            self.on = False
            self.manual = False
        if bit(valor, CMD_MARCHA) and not self.emergencia and not self.manual:
            self.on = True
        if bit(valor, CMD_RESET):
            self.conteo = 0
        if bit(valor, CMD_MAN_ON) and not self.on and not self.emergencia:
            self.manual = True
        if bit(valor, CMD_MAN_OFF):
            self.manual = False

    def escribir_manual(self, valor):
        self.man_bits = valor

    def leer(self):
        if self.emergencia:
            self.on = False
            self.manual = False

        if self.on:
            if self.t_pieza is None:
                self.t_pieza = time.time()
            elif time.time() - self.t_pieza >= 5:
                self.conteo += 1
                self.t_pieza = time.time()
        else:
            self.t_pieza = None

        est = ((self.on << EST_MAQUINA) | (self.on << EST_TALADRO)
               | (self.on << EST_SIERRA) | (self.emergencia << EST_EMERG)
               | (self.manual << EST_MANUAL))

        q = [0, 0]
        if self.on:
            q[0] |= (1 << 7) | (1 << 6) | (1 << 1)
        if self.manual:
            for _, _, b, (qb, qbit) in MANUALES:
                if bit(self.man_bits, b):
                    q[qb] |= 1 << qbit
        return est, self.conteo, q


# ===================== PLC REAL =====================
class PLCReal:
    def __init__(self):
        import snap7  # solo se necesita en modo real
        self.cliente = snap7.client.Client()

    def conectar(self):
        try:
            self.cliente.connect(PLC_IP, RACK, SLOT)
            return self.cliente.get_connected()
        except Exception:
            return False

    def desconectar(self):
        try:
            self.cliente.disconnect()
        except Exception:
            pass

    def escribir_pulsos(self, valor):
        self.cliente.mb_write(BYTE_CMD, 1, bytearray([valor & 0xFF]))

    def escribir_manual(self, valor):
        self.cliente.mb_write(BYTE_MAN, 1, bytearray([valor & 0xFF]))

    def leer(self):
        # MB10..MB17 en una sola lectura: MB11 = estados, MD14 = conteo
        datos = self.cliente.mb_read(BYTE_CMD, 8)
        est = datos[BYTE_EST - BYTE_CMD]
        i = BYTE_CONTEO - BYTE_CMD
        conteo = int.from_bytes(bytes(datos[i:i + 4]), "big", signed=True)
        q = list(self.cliente.ab_read(0, 2))   # QB0, QB1
        return est, conteo, q


# ===================== HILO DE COMUNICACIÓN =====================
class Comunicacion(threading.Thread):
    """Habla con el PLC en segundo plano para que la pantalla nunca se congele."""

    def __init__(self, plc):
        super().__init__(daemon=True)
        self.plc = plc
        self.lock = threading.Lock()
        self.estado = 0
        self.conteo = 0
        self.q = [0, 0]
        self.conectado = False
        self.pulsos = 0          # bits de MB10 pendientes por enviar
        self.pulsos_previos = 0  # para devolverlos a 0 en el ciclo siguiente
        self.manual = 0          # bits de MB12 (botones manuales)

    def pulso(self, n_bit):
        with self.lock:
            self.pulsos |= 1 << n_bit

    def set_manual(self, n_bit, activo):
        with self.lock:
            if activo:
                self.manual |= 1 << n_bit
            else:
                self.manual &= ~(1 << n_bit)

    def soltar_todo(self):
        with self.lock:
            self.manual = 0

    def leer(self):
        with self.lock:
            return self.conectado, self.estado, self.conteo, list(self.q)

    def run(self):
        while True:
            if not self.conectado:
                with self.lock:
                    self.manual = 0
                ok = self.plc.conectar()
                with self.lock:
                    self.conectado = ok
                if not ok:
                    time.sleep(2)
                    continue
            try:
                with self.lock:
                    pulsos, self.pulsos = self.pulsos, 0
                    manual = self.manual
                # Pulsos: se mandan una vez y en el ciclo siguiente se ponen en 0
                if pulsos or self.pulsos_previos:
                    self.plc.escribir_pulsos(pulsos)
                self.pulsos_previos = pulsos
                # Botones manuales + latido, en cada ciclo
                self.plc.escribir_manual(manual | (1 << BIT_VIVO))
                est, conteo, q = self.plc.leer()
                with self.lock:
                    self.estado, self.conteo, self.q = est, conteo, q
            except Exception:
                with self.lock:
                    self.conectado = False
                self.plc.desconectar()
            time.sleep(PERIODO)


# ===================== INTERFAZ =====================
class HMI:
    def __init__(self, root, com, plc):
        self.root = root
        self.com = com
        self.plc = plc
        self.t_titulo = None
        self.t_confirmar = None
        self.pantalla = "auto"
        self.aviso_hasta = 0
        self.aviso_txt = ""
        self.activos = set()     # botones manuales enclavados

        root.title("HMI Taladro / Corte")
        root.configure(bg=FONDO)
        if PANTALLA_COMPLETA:
            root.attributes("-fullscreen", True)
        else:
            root.geometry("1024x600")
        if OCULTAR_CURSOR:
            root.configure(cursor="none")
        root.bind("<Escape>", lambda e: self._salir())

        root.update_idletasks()
        alto = root.winfo_screenheight() if PANTALLA_COMPLETA else 600
        self.f_grande = ("DejaVu Sans", max(14, alto // 15), "bold")
        self.f_media = ("DejaVu Sans", max(11, alto // 26), "bold")
        self.f_chica = ("DejaVu Sans", max(9, alto // 38))

        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._barra_superior()
        self._pestanas()

        self.frm_auto = tk.Frame(root, bg=FONDO)
        self.frm_man = tk.Frame(root, bg=FONDO)
        self.frm_auto.grid(row=2, column=0, sticky="nsew")
        self._pantalla_auto(self.frm_auto)
        self._pantalla_manual(self.frm_man)

        if SIMULADO:
            self.btn_sim = tk.Button(
                root, text="SIM: activar emergencia", font=self.f_chica,
                bg=PANEL, fg=AMARILLO, activebackground=GRIS, relief="flat",
                command=self._sim_emergencia)
            self.btn_sim.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 8))

        # Al arrancar solo con la Raspberry, el escritorio a veces no está
        # listo y ignora la pantalla completa: se vuelve a pedir varias veces.
        if PANTALLA_COMPLETA:
            for ms in (500, 2000, 5000, 10000):
                root.after(ms, self._pantalla_completa)

        self.refrescar()

    def _pantalla_completa(self):
        try:
            self.root.attributes("-fullscreen", True)
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    # ---------------- construcción ----------------
    def _barra_superior(self):
        barra = tk.Frame(self.root, bg=PANEL)
        barra.grid(row=0, column=0, sticky="ew")
        barra.columnconfigure(0, weight=1)
        self.lbl_titulo = tk.Label(barra, text="TALADRO / CORTE", bg=PANEL,
                                   fg=TEXTO, font=self.f_media, anchor="w",
                                   padx=12)
        self.lbl_titulo.grid(row=0, column=0, sticky="w")
        self.lbl_titulo.bind("<ButtonPress-1>", self._titulo_down)
        self.lbl_titulo.bind("<ButtonRelease-1>", self._titulo_up)
        modo = "SIMULADO" if SIMULADO else PLC_IP
        tk.Label(barra, text=modo, bg=PANEL, fg=AMARILLO if SIMULADO else TEXTO,
                 font=self.f_chica, padx=8).grid(row=0, column=1)
        self.lbl_con = tk.Label(barra, text="", bg=PANEL, font=self.f_chica,
                                padx=12)
        self.lbl_con.grid(row=0, column=2)

    def _pestanas(self):
        tabs = tk.Frame(self.root, bg=FONDO)
        tabs.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 0))
        tabs.columnconfigure(0, weight=1, uniform="t")
        tabs.columnconfigure(1, weight=1, uniform="t")
        self.tab_auto = tk.Button(
            tabs, text="AUTOMÁTICO", font=self.f_media, relief="flat", bd=0,
            highlightthickness=0, command=self._ir_auto)
        self.tab_auto.grid(row=0, column=0, sticky="ew", padx=6, ipady=4)
        self.tab_man = tk.Button(
            tabs, text="MANUAL", font=self.f_media, relief="flat", bd=0,
            highlightthickness=0, command=self._ir_manual)
        self.tab_man.grid(row=0, column=1, sticky="ew", padx=6, ipady=4)

    def _pantalla_auto(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        f.rowconfigure(2, weight=2)

        self.lbl_maquina = tk.Label(f, text="", font=self.f_grande, fg="white",
                                    pady=6)
        self.lbl_maquina.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))

        pilotos = tk.Frame(f, bg=FONDO)
        pilotos.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        pilotos.rowconfigure(0, weight=1)
        self.pilotos = {}
        for i, (clave, texto) in enumerate([("banda", "BANDA"),
                                            ("taladro", "TALADRO"),
                                            ("sierra", "SIERRA")]):
            pilotos.columnconfigure(i, weight=1, uniform="p")
            lbl = tk.Label(pilotos, text=texto, font=self.f_media, bg=GRIS,
                           fg=TEXTO)
            lbl.grid(row=0, column=i, sticky="nsew", padx=6)
            self.pilotos[clave] = lbl

        # Contador de piezas
        pilotos.columnconfigure(3, weight=1, uniform="p")
        caja = tk.Frame(pilotos, bg=PANEL)
        caja.grid(row=0, column=3, sticky="nsew", padx=6)
        caja.columnconfigure(0, weight=1)
        caja.rowconfigure(1, weight=1)
        tk.Label(caja, text="PIEZAS", font=self.f_chica, bg=PANEL,
                 fg=TEXTO).grid(row=0, column=0, sticky="ew", pady=(4, 0))
        self.lbl_conteo = tk.Label(caja, text="0", font=self.f_grande,
                                   bg=PANEL, fg=AMARILLO)
        self.lbl_conteo.grid(row=1, column=0, sticky="nsew")
        self.btn_reset = tk.Button(
            caja, text="Reiniciar", font=self.f_chica, bg=GRIS, fg=TEXTO,
            activebackground=AMARILLO, activeforeground="black", relief="flat",
            bd=0, highlightthickness=0, command=self._reset_conteo)
        self.btn_reset.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))

        botones = tk.Frame(f, bg=FONDO)
        botones.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 10))
        botones.columnconfigure(0, weight=1, uniform="b")
        botones.columnconfigure(1, weight=1, uniform="b")
        botones.rowconfigure(0, weight=1)

        self.btn_marcha = tk.Button(
            botones, text="MARCHA", font=self.f_grande, bg=VERDE, fg="white",
            activebackground=VERDE_ON, activeforeground="white",
            disabledforeground="#9a9a9a", relief="flat", bd=0,
            highlightthickness=0, command=lambda: self.com.pulso(CMD_MARCHA))
        self.btn_marcha.grid(row=0, column=0, sticky="nsew", padx=6)

        self.btn_paro = tk.Button(
            botones, text="PARO", font=self.f_grande, bg=ROJO, fg="white",
            activebackground=ROJO_ON, activeforeground="white",
            disabledforeground="#9a9a9a", relief="flat", bd=0,
            highlightthickness=0, command=lambda: self.com.pulso(CMD_PARO))
        self.btn_paro.grid(row=0, column=1, sticky="nsew", padx=6)

    def _pantalla_manual(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        cab = tk.Frame(f, bg=FONDO)
        cab.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        cab.columnconfigure(0, weight=1)
        self.lbl_manual = tk.Label(cab, text="", font=self.f_media, fg="white",
                                   pady=6)
        self.lbl_manual.grid(row=0, column=0, sticky="ew")
        self.btn_activar = tk.Button(
            cab, text="ACTIVAR", font=self.f_media, bg=AZUL, fg="white",
            activebackground=AZUL, relief="flat", bd=0, highlightthickness=0,
            command=lambda: self.com.pulso(CMD_MAN_ON))
        self.btn_activar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.btn_apagar = tk.Button(
            cab, text="APAGAR TODO", font=self.f_media, bg=ROJO, fg="white",
            activebackground=ROJO_ON, activeforeground="white", relief="flat",
            bd=0, highlightthickness=0, command=self._apagar_todo)
        self.btn_apagar.grid(row=0, column=2, sticky="ns", padx=(8, 0))

        rejilla = tk.Frame(f, bg=FONDO)
        rejilla.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 6))
        for c in range(3):
            rejilla.columnconfigure(c, weight=1, uniform="m")
        rejilla.rowconfigure(0, weight=1)
        rejilla.rowconfigure(1, weight=1)

        self.btn_man = {}
        for i, (clave, texto, n_bit, _) in enumerate(MANUALES):
            fila, col = divmod(i, 3)
            lbl = tk.Label(rejilla, text=texto, font=self.f_media, bg=GRIS,
                           fg=TEXTO, relief="flat")
            lbl.grid(row=fila, column=col, sticky="nsew", padx=6, pady=6)
            lbl.bind("<ButtonPress-1>",
                     lambda e, b=n_bit, k=clave: self._man_toggle(b, k))
            self.btn_man[clave] = lbl

    # ---------------- navegación ----------------
    def _ir_auto(self):
        self._apagar_todo()
        self.com.pulso(CMD_MAN_OFF)
        self.pantalla = "auto"
        self.frm_man.grid_remove()
        self.frm_auto.grid(row=2, column=0, sticky="nsew")

    def _ir_manual(self):
        conectado, est, _, _ = self.com.leer()
        if conectado and bit(est, EST_MAQUINA):
            self._aviso("Detén la máquina primero")
            return
        self.com.pulso(CMD_MAN_ON)
        self.pantalla = "manual"
        self.frm_auto.grid_remove()
        self.frm_man.grid(row=2, column=0, sticky="nsew")

    def _aviso(self, txt):
        self.aviso_txt = txt
        self.aviso_hasta = time.time() + 3

    # ---------------- botones manuales (enclavados) ----------------
    def _man_toggle(self, n_bit, clave):
        """Un toque enciende, otro toque apaga. Máximo MAX_MANUAL a la vez."""
        if clave in self.activos:
            self.activos.discard(clave)
            self.com.set_manual(n_bit, False)
            return
        conectado, est, _, _ = self.com.leer()
        if not (conectado and bit(est, EST_MANUAL)):
            return
        if len(self.activos) >= MAX_MANUAL:
            self._aviso(f"Máximo {MAX_MANUAL} a la vez")
            return
        self.activos.add(clave)
        self.com.set_manual(n_bit, True)

    def _apagar_todo(self):
        self.activos.clear()
        self.com.soltar_todo()

    # ---------------- otros ----------------
    def _salir(self):
        self.com.soltar_todo()
        self.com.pulso(CMD_MAN_OFF)
        time.sleep(0.3)   # da tiempo a que salga el último envío
        self.root.destroy()

    def _titulo_down(self, _):
        self.t_titulo = time.time()

    def _titulo_up(self, _):
        if self.t_titulo and time.time() - self.t_titulo >= 3:
            self._salir()
            return
        self.t_titulo = None

    def _reset_conteo(self):
        ahora = time.time()
        if self.t_confirmar and ahora - self.t_confirmar < 3:
            self.com.pulso(CMD_RESET)
            self.t_confirmar = None
        else:
            self.t_confirmar = ahora

    def _sim_emergencia(self):
        self.plc.emergencia = not self.plc.emergencia
        self.btn_sim.config(text="SIM: quitar emergencia" if self.plc.emergencia
                            else "SIM: activar emergencia")

    # ---------------- refresco ----------------
    def refrescar(self):
        conectado, est, conteo, q = self.com.leer()
        on = conectado and bit(est, EST_MAQUINA)
        emerg = conectado and bit(est, EST_EMERG)
        manual = conectado and bit(est, EST_MANUAL)
        hay_aviso = time.time() < self.aviso_hasta

        if conectado:
            self.lbl_con.config(text="● CONECTADO", fg=VERDE_ON)
        else:
            self.lbl_con.config(text="● SIN CONEXIÓN", fg=ROJO_ON)

        # Pestañas
        es_auto = self.pantalla == "auto"
        self.tab_auto.config(bg=AZUL if es_auto else GRIS_OSC,
                             fg="white" if es_auto else "#9a9a9a",
                             activebackground=AZUL)
        self.tab_man.config(bg=AZUL if not es_auto else GRIS_OSC,
                            fg="white" if not es_auto else "#9a9a9a",
                            activebackground=AZUL)

        # ---- Automático ----
        if hay_aviso:
            self.lbl_maquina.config(text=self.aviso_txt, bg=AMARILLO, fg="black")
        elif not conectado:
            self.lbl_maquina.config(text="SIN COMUNICACIÓN CON EL PLC", bg=GRIS,
                                    fg="white")
        elif emerg:
            self.lbl_maquina.config(text="PARO DE EMERGENCIA", bg=ROJO,
                                    fg="white")
        elif manual:
            self.lbl_maquina.config(text="EN MODO MANUAL", bg=AZUL, fg="white")
        elif on:
            self.lbl_maquina.config(text="MÁQUINA ENCENDIDA", bg=VERDE,
                                    fg="white")
        else:
            self.lbl_maquina.config(text="MÁQUINA APAGADA", bg=GRIS, fg="white")

        self.pilotos["banda"].config(
            bg=VERDE_ON if conectado and bit(q[Q_BANDA[0]], Q_BANDA[1])
            else GRIS)
        self.pilotos["taladro"].config(
            bg=VERDE_ON if conectado and bit(est, EST_TALADRO) else GRIS)
        self.pilotos["sierra"].config(
            bg=VERDE_ON if conectado and bit(est, EST_SIERRA) else GRIS)

        self.lbl_conteo.config(text=str(conteo) if conectado else "--")
        confirmando = (self.t_confirmar is not None
                       and time.time() - self.t_confirmar < 3)
        if not confirmando:
            self.t_confirmar = None
        self.btn_reset.config(
            text="¿Seguro? Toca otra vez" if confirmando else "Reiniciar",
            bg=AMARILLO if confirmando else GRIS,
            fg="black" if confirmando else TEXTO,
            state="normal" if conectado else "disabled")

        puede_marcha = conectado and not emerg and not on and not manual
        self.btn_marcha.config(state="normal" if puede_marcha else "disabled",
                               bg=VERDE if puede_marcha else GRIS)
        self.btn_paro.config(state="normal" if conectado else "disabled",
                             bg=ROJO if conectado else GRIS)

        # ---- Manual ----
        if not manual and self.activos:
            # El PLC sacó el modo manual (Stop, emergencia, sin conexión...)
            self._apagar_todo()

        if hay_aviso and not es_auto:
            self.lbl_manual.config(text=self.aviso_txt, bg=AMARILLO, fg="black")
        elif not conectado:
            self.lbl_manual.config(text="SIN COMUNICACIÓN CON EL PLC", bg=GRIS,
                                   fg="white")
        elif emerg:
            self.lbl_manual.config(text="PARO DE EMERGENCIA", bg=ROJO, fg="white")
        elif manual:
            self.lbl_manual.config(
                text=f"MANUAL ACTIVO · {len(self.activos)}/{MAX_MANUAL}",
                bg=AZUL, fg="white")
        elif on:
            self.lbl_manual.config(text="Detén la máquina primero",
                                   bg=AMARILLO, fg="black")
        else:
            self.lbl_manual.config(text="MANUAL INACTIVO — toca ACTIVAR",
                                   bg=GRIS, fg="white")

        if manual or not conectado or emerg or on:
            self.btn_activar.grid_remove()
        else:
            self.btn_activar.grid()
        if manual:
            self.btn_apagar.grid()
        else:
            self.btn_apagar.grid_remove()

        for clave, _, _, (qb, qbit) in MANUALES:
            salida_on = conectado and bit(q[qb], qbit)
            if salida_on:
                color, fg = VERDE_ON, "black"
            elif clave in self.activos:
                color, fg = AMARILLO, "black"   # pedido, esperando al PLC
            elif manual:
                color, fg = GRIS, TEXTO
            else:
                color, fg = GRIS_OSC, "#6f7380"
            self.btn_man[clave].config(bg=color, fg=fg)

        self.root.after(100, self.refrescar)


def main():
    plc = PLCSimulado() if SIMULADO else PLCReal()
    com = Comunicacion(plc)
    com.start()
    root = tk.Tk()
    HMI(root, com, plc)
    root.mainloop()


if __name__ == "__main__":
    main()