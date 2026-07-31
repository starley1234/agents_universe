"""Тесты инструментов физических и инженерных расчётов (physics.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.local.physics import build_physics_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Инструменты физических, инженерных и акустических расчётов (physics.*)")
    tools = {t.name: t for t in build_physics_tools()}
    check("зарегистрировано 20 инструментов physics", len(tools) == 20)

    # 1) calc_strength
    res_str = tools["physics.calc_strength"].execute(
        load_n=10000.0, area_mm2=50.0, yield_strength_mpa=250.0, beam_length_mm=100.0, modulus_gpa=200.0, inertia_mm4=1000.0
    )
    check("calc_strength считает механическое напряжение (200.0 МПа)", "200.0 МПа" in res_str)
    check("calc_strength рассчитывает запас прочности", "запаса прочности: **1.25**" in res_str)
    check("calc_strength считает прогиб консоли", "Максимальный прогиб" in res_str)

    # 2) calc_em_field
    res_em = tools["physics.calc_em_field"].execute(
        current_a=10.0, distance_mm=20.0, turns_count=100, solenoid_length_mm=50.0
    )
    check("calc_em_field считает поле вокруг провода", "вокруг провода" in res_em and "0.1 мТл" in res_em)
    check("calc_em_field считает индукцию внутри соленоида", "внутри соленоида" in res_em and "25.1327 мТл" in res_em)

    # 3) calc_antenna
    res_ant = tools["physics.calc_antenna"].execute(
        freq_mhz=433.92, antenna_type="dipole", velocity_factor=0.95
    )
    check("calc_antenna считает длину волны (0.6909 м)", "0.6909 м" in res_ant)
    check("calc_antenna рассчитывает полуволновой диполь", "Полуволновой диполь" in res_ant and "32.82 см" in res_ant)

    # 4) calc_airflow
    res_air = tools["physics.calc_airflow"].execute(
        velocity_m_s=20.0, char_length_m=0.5, drag_coeff=0.3, frontal_area_m2=2.0
    )
    check("calc_airflow вычисляет число Рейнольдса и турбулентность", "Турбулентное течение" in res_air)
    check("calc_airflow считает силу сопротивления Drag Force", "147.0 Н" in res_air)

    # 5) calc_acoustics
    res_snd = tools["physics.calc_acoustics"].execute(
        freq_hz=440.0, medium="air", temperature_c=20.0, pressure_pa=0.2, pipe_length_m=0.5, pipe_closed_end=True
    )
    check("calc_acoustics считает скорость звука (343.21 м/с)", "343.21 м/с" in res_snd)
    check("calc_acoustics считает SPL дБ при давлении 0.2 Па (80.0 дБ)", "80.0 дБ" in res_snd)
    check("calc_acoustics считает резонанс закрытой трубы λ/4", "171.6 Гц" in res_snd)

    # 6) calc_fatigue_life
    res_fat = tools["physics.calc_fatigue_life"].execute(
        stress_amp_mpa=200.0, endurance_limit_mpa=150.0, ult_tensile_mpa=400.0
    )
    check("calc_fatigue_life оценивает циклическую долговечность", "циклов" in res_fat)

    # 7) calc_bolt_torque
    res_bolt = tools["physics.calc_bolt_torque"].execute(
        bolt_diameter_mm=8.0, property_class="8.8", friction_coeff_k=0.2
    )
    check("calc_bolt_torque считает момент затяжки болта М8", "Н·м" in res_bolt and "кН" in res_bolt)

    # 8) calc_rf_link_budget
    res_rf = tools["physics.calc_rf_link_budget"].execute(
        freq_mhz=868.0, tx_power_dbm=14.0, distance_km=2.0, tx_gain_dbi=2.15, rx_gain_dbi=2.15, rx_sensitivity_dbm=-110.0
    )
    check("calc_rf_link_budget считает FSPL и Fade Margin", "FSPL" in res_rf and "Fade Margin" in res_rf)

    # 9) calc_coaxial_cable
    res_coax = tools["physics.calc_coaxial_cable"].execute(
        inner_diam_mm=1.0, outer_diam_mm=3.5, dielectric_constant=2.1
    )
    check("calc_coaxial_cable считает волновое сопротивление Z0 и ёмкость", "Z0" in res_coax and "пФ/м" in res_coax)

    # 10) calc_fan_cooling
    res_fan = tools["physics.calc_fan_cooling"].execute(
        heat_power_watts=150.0, max_temp_rise_c=15.0
    )
    check("calc_fan_cooling считает расход вентилятора CFM", "CFM" in res_fan and "м³/ч" in res_fan)

    # 11) calc_pipe_pressure_drop
    res_pipe = tools["physics.calc_pipe_pressure_drop"].execute(
        airflow_m3_h=500.0, pipe_diam_mm=200.0, pipe_length_m=10.0
    )
    check("calc_pipe_pressure_drop считает потери давления в Па", "Потери давления на трение" in res_pipe)

    # 12) calc_sound_barrier
    res_bar = tools["physics.calc_sound_barrier"].execute(
        surface_mass_kg_m2=50.0, freq_hz=500.0
    )
    check("calc_sound_barrier считает звукоизоляцию Rw в дБ", "Затухание звуковой волны" in res_bar)

    # 13) calc_helmholtz_resonator
    res_helm = tools["physics.calc_helmholtz_resonator"].execute(
        volume_liter=15.0, port_diam_mm=50.0, port_length_mm=100.0
    )
    check("calc_helmholtz_resonator считает частоту f0", "Собственная резонансная частота" in res_helm)

    # 14) calc_antenna_vswr
    res_vswr = tools["physics.calc_antenna_vswr"].execute(
        forward_power_w=10.0, reflected_power_w=0.4
    )
    check("calc_antenna_vswr считает КСВ и Return Loss", "Коэффициент стоячей волны (VSWR" in res_vswr and "1.5:1" in res_vswr)

    # 15) calc_antenna_matching_network
    res_match = tools["physics.calc_antenna_matching_network"].execute(
        freq_mhz=433.92, antenna_r_ohm=25.0
    )
    check("calc_antenna_matching_network рассчитывает Г-образную LC-цепь", "Индуктивность:" in res_match and "Ёмкость:" in res_match)

    # 16) calc_yagi_uda_antenna
    res_yagi_ph = tools["physics.calc_yagi_uda_antenna"].execute(
        freq_mhz=433.92, elements_count=5
    )
    check("calc_yagi_uda_antenna рассчитывает размеры вибратора, рефлектора и директоров", "Рефлектор:" in res_yagi_ph and "Активный вибратор" in res_yagi_ph)

    # 17) calc_patch_antenna
    res_patch = tools["physics.calc_patch_antenna"].execute(
        freq_mhz=2400.0, dielectric_constant=4.4
    )
    check("calc_patch_antenna вычисляет размеры микрополосковой антенны W и L", "Ширина патча (W):" in res_patch and "Длина патча (L):" in res_patch)

    # 18) calc_propeller_thrust_power
    res_prop_tp = tools["physics.calc_propeller_thrust_power"].execute(
        diameter_mm=250.0, rpm=3000.0, pitch_mm=120.0
    )
    check("calc_propeller_thrust_power считает тягу, мощность и Tip Speed", "Скорость кончика лопасти" in res_prop_tp and "Расчётная тяга" in res_prop_tp)

    # 19) calc_propeller_noise
    res_prop_noise = tools["physics.calc_propeller_noise"].execute(
        diameter_mm=200.0, rpm=2500.0, blades_count=5
    )
    check("calc_propeller_noise оценивает шум и даёт рекомендации по снижению", "Blade Pass Frequency" in res_prop_noise and "МАЛОШУМНЫЙ ПРОПЕЛЛЕР" in res_prop_noise)

    # 20) calc_low_noise_blade_geometry
    res_blade_geom = tools["physics.calc_low_noise_blade_geometry"].execute(
        diameter_mm=250.0, design_rpm=2500.0
    )
    check("calc_low_noise_blade_geometry распределяет углы крутки и хорду", "Угол крутки" in res_blade_geom and "Хорда" in res_blade_geom)

    return summary("Тесты физики и инженерных расчётов")


def test_physics_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
