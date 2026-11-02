from machine import Pin, I2S
import gc
import math
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
    except KeyboardInterrupt:
        # Fail safe: red on, sound off
        set_light(red)
        audio.deinit()


if __name__ == "__main__":
    main()
