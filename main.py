from machine import Pin, I2C, I2S
import gc
import math
import neopixel
import random
import time

# ---------------------------------------------------------------
# LEDs (GP numbers; physical pins 4/5/6, with pin 3 as GND)
# ---------------------------------------------------------------
red = Pin(2, Pin.OUT)
yellow = Pin(3, Pin.OUT)
green = Pin(4, Pin.OUT)
LEDS = (red, yellow, green)

# (LED, duration in ms) - runs on its own clock, independent of the music
LIGHT_SEQ = (
    (red, 5000),
    (green, 5000),
    (yellow, 2000),
)

# ---------------------------------------------------------------
# Motor (DRV2605L haptic driver -> small DC/ERM motor)
# I2C0: SDA=GP12 (pin 16), SCL=GP13 (pin 17), VIN=3V3, address 0x5A
# ---------------------------------------------------------------
DRV_ADDR = 0x5A
MOTOR_KICK_RTP = 0x19        # start-up drive, 0x00-0x7F (0x19 = ~20%) to overcome stiction
MOTOR_KICK_MS = 50      # how long to hold the start-up drive
MOTOR_HOLD_RTP = 0x0F        # running drive once spinning (0x0F = ~12%, "low speed")
MOTOR_REKICK_MS = 10000      # repeat the kick-start this often, in case the motor quit
MOTOR_ODCLAMP = 0x5D         # open-loop full-scale drive = 93 * 21.6 mV = ~2.0 V

# ---------------------------------------------------------------
# NeoPixels: data on GP28 (pin 34), VCC = 5 V, shared GND
# ---------------------------------------------------------------
NEO_PIN = 28
NEO_COUNT = 1
NEO_BRIGHTNESS = 0.5         # 0.0 - 1.0
NEO_STEP_MS = 40             # colour advances one of 255 wheel steps every 40 ms (~10 s per cycle)

# ---------------------------------------------------------------
# Audio settings (MAX98357A: BCLK=GP17, LRC=GP18, DIN=GP16)
# ---------------------------------------------------------------
SAMPLE_RATE = 16000
VOLUME = 0.3                # 0.0 - 1.0 (GAIN floating = ~9 dB; lower if it distorts)
STEP_SAMPLES = 1920          # one 16th note: 120 ms -> 125 BPM
CHUNK_BYTES = 640            # 20 ms of audio per write (divides a step evenly)

PEAK = int(32767 * VOLUME)
LEAD_AMP = int(PEAK * 0.35)
BASS_AMP = int(PEAK * 0.35)
KICK_AMP = int(PEAK * 0.50)
HAT_AMP = int(PEAK * 0.15)
DUTY = 0x4000                # 25% pulse-wave lead

# ---------------------------------------------------------------
# The tune: 2 bars (32 sixteenth-note steps) in A minor, Am - F - C - G
# ---------------------------------------------------------------
G4, A4, B4, C5, D5, E5, F5, G5 = 392.00, 440.00, 493.88, 523.25, 587.33, 659.25, 698.46, 783.99

LEAD = (
    E5, 0, E5, C5, 0, A4, C5, E5,    # Am
    F5, 0, F5, C5, 0, A4, C5, F5,    # F
    E5, 0, E5, G5, 0, E5, C5, E5,    # C
    D5, 0, D5, B4, 0, G4, B4, D5,    # G
)

BASS_ROOTS = (110.00, 87.31, 130.81, 98.00)   # A2, F2, C3, G2
BASS_PATTERN = (1, 0, 2, 1, 0, 1, 2, 0)       # octave multipliers, 0 = rest
BASS = tuple(BASS_ROOTS[s // 8] * BASS_PATTERN[s % 8] for s in range(32))

STEPS = len(LEAD)


# ---------------------------------------------------------------
# Sound building blocks (computed once at startup)
# ---------------------------------------------------------------
def build_kick():
    n = int(SAMPLE_RATE * 0.14)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        freq = 45 + 120 * math.exp(-t / 0.03)       # pitch drops fast
        phase += freq / SAMPLE_RATE
        env = math.exp(-t / 0.05)
        if i > n - 100:
            env *= (n - i) / 100                     # taper to avoid a click
        out.append(int(KICK_AMP * env * math.sin(2 * math.pi * phase)))
    return out


def build_hat():
    n = int(SAMPLE_RATE * 0.04)
    out = []
    for i in range(n):
        env = (1 - i / n) ** 2
        noise = random.getrandbits(8) - 128
        out.append(int(noise * HAT_AMP * env / 128))
    return out


def build_envelopes():
    lead = []
    bass = []
    for i in range(STEP_SAMPLES):
        attack = min(1.0, i / 32)
        lead.append(int(256 * attack * (1 - i / STEP_SAMPLES)))      # plucky decay
        release = min(1.0, (STEP_SAMPLES - 1 - i) / 96)
        bass.append(int(256 * attack * release))                      # sustained, gated
    return lead, bass


def render():
    """Mix the whole 2-bar loop into a 16-bit mono buffer."""
    kick = build_kick()
    hat = build_hat()
    kick_len = len(kick)
    hat_len = len(hat)
    env_lead, env_bass = build_envelopes()

    buf = bytearray(STEP_SAMPLES * STEPS * 2)
    pos = 0

    for step in range(STEPS):
        lf = LEAD[step]
        bf = BASS[step]
        l_inc = int(lf * 65536 / SAMPLE_RATE) if lf else 0
        b_inc = int(bf * 65536 / SAMPLE_RATE) if bf else 0
        l_ph = 0
        b_ph = 0
        kick_on = (step % 4 == 0)     # four on the floor
        hat_on = (step % 4 == 2)      # off-beat hats

        for i in range(STEP_SAMPLES):
            s = 0
            if l_inc:
                l_ph = (l_ph + l_inc) & 0xFFFF
                v = (LEAD_AMP * env_lead[i]) >> 8
                if l_ph < DUTY:
                    s += v
                else:
                    s -= v
            if b_inc:
                b_ph = (b_ph + b_inc) & 0xFFFF
                saw = ((b_ph >> 8) - 128) * BASS_AMP >> 7
                s += (saw * env_bass[i]) >> 8
            if kick_on and i < kick_len:
                s += kick[i]
            if hat_on and i < hat_len:
                s += hat[i]

            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            buf[pos] = s & 255
            buf[pos + 1] = (s >> 8) & 255
            pos += 2

    return buf


def set_light(light):
    # Turn the new light ON first, then the others OFF,
    # so one LED is always lit.
    light.value(1)
    for other in LEDS:
        if other is not light:
            other.value(0)


def motor_init():
    """Set up the DRV2605L for an ERM motor in open-loop real-time-playback mode.
    Returns (i2c, ok). The motor is optional: on any failure the music and
    lights keep running without it."""
    try:
        i2c = I2C(0, sda=Pin(12), scl=Pin(13), freq=400000)
        chip = i2c.readfrom_mem(DRV_ADDR, 0x00, 1)[0] >> 5   # 3 = DRV2605, 7 = DRV2605L
        if chip not in (3, 7):
            raise OSError("unexpected DRV260x id %d" % chip)
        i2c.writeto_mem(DRV_ADDR, 0x01, b"\x00")              # leave standby
        fb = i2c.readfrom_mem(DRV_ADDR, 0x1A, 1)[0]
        i2c.writeto_mem(DRV_ADDR, 0x1A, bytes([fb & 0x7F]))   # N_ERM_LRA = 0 -> ERM
        c3 = i2c.readfrom_mem(DRV_ADDR, 0x1D, 1)[0]
        i2c.writeto_mem(DRV_ADDR, 0x1D, bytes([c3 | 0x20]))   # ERM open-loop (no calibration)
        i2c.writeto_mem(DRV_ADDR, 0x17, bytes([MOTOR_ODCLAMP]))
        i2c.writeto_mem(DRV_ADDR, 0x02, b"\x00")              # RTP level 0 = stopped
        i2c.writeto_mem(DRV_ADDR, 0x01, b"\x05")              # mode: real-time playback
        return i2c, True
    except Exception as e:
        print("motor disabled:", e)
        return None, False


def motor_set(i2c, level):
    i2c.writeto_mem(DRV_ADDR, 0x02, bytes([level]))


def wheel(pos):
    """Map 0-254 around the colour wheel (red -> green -> blue -> red)."""
    if pos < 85:
        return (255 - pos * 3, pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (0, 255 - pos * 3, pos * 3)
    pos -= 170
    return (pos * 3, 0, 255 - pos * 3)


def neo_show(np, pos):
    r, g, b = wheel(pos)
    color = (int(r * NEO_BRIGHTNESS), int(g * NEO_BRIGHTNESS), int(b * NEO_BRIGHTNESS))
    for i in range(NEO_COUNT):
        np[i] = color
    np.write()


def main():
    set_light(red)                 # keep a light on while the tune renders
    gc.collect()
    buf = render()
    mv = memoryview(buf)
    gc.collect()

    audio = I2S(
        0,
        sck=Pin(17),
        ws=Pin(18),
        sd=Pin(16),
        mode=I2S.TX,
        bits=16,
        format=I2S.MONO,
        rate=SAMPLE_RATE,
        ibuf=8000,
    )

    light_idx = 0
    light_start = time.ticks_ms()
    set_light(LIGHT_SEQ[light_idx][0])

    # Motor: kick-start once, then run continuously at the low hold level
    motor, motor_ok = motor_init()
    motor_kicking = False
    motor_start = time.ticks_ms()
    if motor_ok:
        motor_set(motor, MOTOR_KICK_RTP)
        motor_kicking = True

    # NeoPixels: slow colour cycle at 50% brightness, on its own timer
    neo = neopixel.NeoPixel(Pin(NEO_PIN), NEO_COUNT)
    neo_pos = 0
    neo_last = time.ticks_ms()
    neo_show(neo, neo_pos)

    try:
        while True:
            for off in range(0, len(buf), CHUNK_BYTES):
                # write() blocks when the buffer is full, which paces this loop
                audio.write(mv[off:off + CHUNK_BYTES])

                # Traffic light runs on its own timer, separate from the music
                if time.ticks_diff(time.ticks_ms(), light_start) >= LIGHT_SEQ[light_idx][1]:
                    light_start = time.ticks_add(light_start, LIGHT_SEQ[light_idx][1])
                    light_idx = (light_idx + 1) % len(LIGHT_SEQ)
                    set_light(LIGHT_SEQ[light_idx][0])

                if time.ticks_diff(time.ticks_ms(), neo_last) >= NEO_STEP_MS:
                    neo_last = time.ticks_add(neo_last, NEO_STEP_MS)
                    neo_pos = (neo_pos + 1) % 255
                    neo_show(neo, neo_pos)

                if motor_ok:
                    now = time.ticks_ms()
                    level = None
                    if motor_kicking:
                        if time.ticks_diff(now, motor_start) >= MOTOR_KICK_MS:
                            motor_kicking = False     # up to speed: drop to the hold level
                            level = MOTOR_HOLD_RTP
                    elif time.ticks_diff(now, motor_start) >= MOTOR_REKICK_MS:
                        motor_kicking = True          # periodic re-kick in case it stalled
                        motor_start = now
                        level = MOTOR_KICK_RTP
                    if level is not None:
                        try:
                            motor_set(motor, level)
                        except OSError as e:
                            print("motor disabled:", e)   # keep the music going
                            motor_ok = False
    except KeyboardInterrupt:
        # Fail safe: red on, sound off, motor off
        set_light(red)
        audio.deinit()
        neo.fill((0, 0, 0))
        neo.write()
        if motor_ok:
            try:
                motor_set(motor, 0)
            except OSError:
                pass


if __name__ == "__main__":
    main()
