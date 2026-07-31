"""Инструменты физических, инженерных и акустических расчётов (physics.*).

Обеспечивают точные математические и инженерные расчёты без «догадок» LLM:
  1. Расчёты прочности (механическое напряжение σ, запас прочности, прогиб балок, усталость, болтовые соединения).
  2. Расчёты электромагнитных полей и радиосвязи (магнитная индукция B, бюджет радиолинии RF Link Budget, коаксиальные кабели).
  3. Расчёты антенн (длина волны, геометрия диполя/патч-антенны, коэффициент усиления).
  4. Расчёты воздушных потоков / аэродинамики (число Рейнольдса Re, режим течения, сила лобового сопротивления, тепловое охлаждение CFM, потери давления).
  5. Расчёты звуковых волн / акустики (скорость звука в среде, длина волны, уровень звукового давления дБ, резонанс трубы, звукоизоляция, резонатор Гельмгольца).
"""
from __future__ import annotations

import math
from typing import Any

from ..core import Tool, ToolError


def build_physics_tools() -> list[Tool]:
    """Собрать полный набор инструментов для физических, инженерных и акустических расчётов."""

    def calc_strength(
        load_n: float,
        area_mm2: float,
        yield_strength_mpa: float = 250.0,
        beam_length_mm: float = 0.0,
        modulus_gpa: float = 200.0,
        inertia_mm4: float = 0.0,
    ) -> str:
        if area_mm2 <= 0:
            raise ToolError("Площадь сечения (area_mm2) должна быть положительной")

        stress_mpa = round(load_n / area_mm2, 3)
        safety_factor = (
            round(yield_strength_mpa / stress_mpa, 2)
            if stress_mpa > 0
            else 999.0
        )
        is_safe = safety_factor >= 1.5

        res = [
            "### Инженерный расчёт прочности:",
            f"- Напряжение в сечении (σ): **{stress_mpa} МПа** (при нагрузке {load_n} Н на {area_mm2} мм²)",
            f"- Предел текучести материала: {yield_strength_mpa} МПа",
            f"- Коэффициент запаса прочности: **{safety_factor}** -> {'✓ БЕЗОПАСНО' if is_safe else '⚠ ВНИМАНИЕ: риск разрушения / пластической деформации'}",
        ]

        if beam_length_mm > 0 and inertia_mm4 > 0 and modulus_gpa > 0:
            modulus_mpa = modulus_gpa * 1000.0
            deflection_mm = round(
                (load_n * (beam_length_mm ** 3))
                / (3.0 * modulus_mpa * inertia_mm4),
                4,
            )
            res.append(f"- Максимальный прогиб консоли (L={beam_length_mm} мм): **{deflection_mm} мм**")

        return "\n".join(res)

    def calc_em_field(
        current_a: float,
        distance_mm: float = 10.0,
        turns_count: int = 0,
        solenoid_length_mm: float = 0.0,
        core_permeability: float = 1.0,
    ) -> str:
        mu_0 = 4.0 * math.pi * 1e-7
        res = ["### Расчёт электромагнитного поля:"]

        if distance_mm > 0:
            dist_m = distance_mm / 1000.0
            b_wire_t = (mu_0 * current_a) / (2.0 * math.pi * dist_m)
            b_wire_mt = round(b_wire_t * 1000.0, 4)
            res.append(f"- Магнитная индукция вокруг провода (на расстоянии {distance_mm} мм, ток {current_a} А): **{b_wire_mt} мТл**")

        if turns_count > 0 and solenoid_length_mm > 0:
            length_m = solenoid_length_mm / 1000.0
            b_sol_t = mu_0 * core_permeability * (turns_count / length_m) * current_a
            b_sol_mt = round(b_sol_t * 1000.0, 4)
            res.append(
                f"- Магнитная индукция внутри соленоида (витков N={turns_count}, L={solenoid_length_mm} мм, μ={core_permeability}): **{b_sol_mt} мТл**"
            )

        return "\n".join(res)

    def calc_antenna(
        freq_mhz: float,
        antenna_type: str = "dipole",
        velocity_factor: float = 0.95,
    ) -> str:
        if freq_mhz <= 0:
            raise ToolError("Частота (freq_mhz) должна быть положительной")

        c = 299.792458
        wavelength_m = round(c / freq_mhz, 4)
        wavelength_cm = round(wavelength_m * 100.0, 2)

        ant_type = (antenna_type or "dipole").lower()
        res = [
            f"### Антенный расчёт для частоты {freq_mhz} МГц:",
            f"- Длина волны в вакууме (λ): **{wavelength_m} м** ({wavelength_cm} см)",
        ]

        if ant_type in ("dipole", "half_wave"):
            dipole_len_cm = round((wavelength_cm / 2.0) * velocity_factor, 2)
            arm_len_cm = round(dipole_len_cm / 2.0, 2)
            res.append(f"- Тип: **Полуволновой диполь (λ/2)** с коэфф. укорочения {velocity_factor}")
            res.append(f"- Общая длина вибратора: **{dipole_len_cm} см**")
            res.append(f"- Длина каждого плеча: **{arm_len_cm} см**")
            res.append("- Расчётный коэффициент усиления: ~2.15 dBi")
        elif ant_type in ("quarter_wave", "monopole"):
            monopole_len_cm = round((wavelength_cm / 4.0) * velocity_factor, 2)
            res.append(f"- Тип: **Четвертьволновой штырь (λ/4)**")
            res.append(f"- Длина штыря: **{monopole_len_cm} см**")
            res.append("- Требуется земляная плоскость (Ground Plane) или противовесы")
        else:
            res.append(f"- Тип антенны '{antenna_type}' — используйте базовую длину волны λ = {wavelength_cm} см")

        return "\n".join(res)

    def calc_airflow(
        velocity_m_s: float,
        char_length_m: float = 0.1,
        air_density: float = 1.225,
        kinematic_viscosity: float = 1.5e-5,
        drag_coeff: float = 0.0,
        frontal_area_m2: float = 0.0,
    ) -> str:
        if velocity_m_s < 0 or char_length_m <= 0:
            raise ToolError("Скорость и характерный размер должны быть положительными")

        reynolds = round((velocity_m_s * char_length_m) / kinematic_viscosity, 1)
        flow_regime = (
            "Ламинарное течение (Re < 2300)"
            if reynolds < 2300
            else "Переходный режим (2300 ≤ Re ≤ 4000)"
            if reynolds <= 4000
            else "Турбулентное течение (Re > 4000)"
        )

        res = [
            "### Аэродинамический расчёт воздушного потока:",
            f"- Скорость потока: {velocity_m_s} м/с, характерный размер L = {char_length_m} м",
            f"- Число Рейнольдса (Re): **{reynolds}** -> **{flow_regime}**",
        ]

        if drag_coeff > 0 and frontal_area_m2 > 0:
            drag_force_n = round(
                0.5 * air_density * (velocity_m_s ** 2) * drag_coeff * frontal_area_m2,
                4,
            )
            res.append(f"- Сила лобового сопротивления (Drag Force, Cd={drag_coeff}, A={frontal_area_m2} м²): **{drag_force_n} Н**")

        return "\n".join(res)

    def calc_acoustics(
        freq_hz: float = 1000.0,
        medium: str = "air",
        temperature_c: float = 20.0,
        pressure_pa: float = 0.0,
        pipe_length_m: float = 0.0,
        pipe_closed_end: bool = False,
    ) -> str:
        if freq_hz <= 0:
            raise ToolError("Частота звука (freq_hz) должна быть положительной")

        med = (medium or "air").lower()
        if med == "air":
            sound_speed = round(331.3 * math.sqrt(1.0 + (temperature_c / 273.15)), 2)
        elif med == "water":
            sound_speed = 1482.0
        elif med == "steel":
            sound_speed = 5900.0
        else:
            sound_speed = 343.0

        wavelength_m = round(sound_speed / freq_hz, 4)
        wavelength_cm = round(wavelength_m * 100.0, 2)

        res = [
            f"### Акустический расчёт (среда: {medium}, T={temperature_c}°C):",
            f"- Скорость звука в среде (c): **{sound_speed} м/с**",
            f"- Длина звуковой волны (λ при {freq_hz} Гц): **{wavelength_m} м** ({wavelength_cm} см)",
        ]

        if pressure_pa > 0:
            p0 = 2e-5
            spl_db = round(20.0 * math.log10(pressure_pa / p0), 1)
            res.append(f"- Уровень звукового давления (SPL при давлении {pressure_pa} Па): **{spl_db} дБ**")

        if pipe_length_m > 0:
            if pipe_closed_end:
                f1 = round(sound_speed / (4.0 * pipe_length_m), 2)
                res.append(f"- Фундаментальная резонансная частота трубы (закрыта с 1 конца, L={pipe_length_m} м): **{f1} Гц** (λ/4 резонатор)")
            else:
                f1 = round(sound_speed / (2.0 * pipe_length_m), 2)
                res.append(f"- Фундаментальная резонансная частота трубы (открыта с 2 концов, L={pipe_length_m} м): **{f1} Гц** (λ/2 резонатор)")

        return "\n".join(res)

    def calc_fatigue_life(
        stress_amp_mpa: float,
        endurance_limit_mpa: float = 150.0,
        ult_tensile_mpa: float = 400.0,
    ) -> str:
        if stress_amp_mpa <= 0:
            raise ToolError("Амплитуда напряжения должна быть положительной")

        if stress_amp_mpa <= endurance_limit_mpa:
            return (
                f"### Усталостный расчёт (S-N Curve):\n"
                f"- Амплитуда напряжения: {stress_amp_mpa} МПа\n"
                f"- Предел выносливости: {endurance_limit_mpa} МПа\n"
                f"- Прогноз: **Бесконечная долговечность (> 10^7 циклов)** (напряжение ниже предела выносливости)"
            )

        if stress_amp_mpa >= ult_tensile_mpa:
            return (
                f"### Усталостный расчёт:\n"
                f"- Амплитуда напряжения ({stress_amp_mpa} МПа) превышает предел прочности ({ult_tensile_mpa} МПа).\n"
                f"- Прогноз: **Мгновенное разрушение (1 цикл)**"
            )

        # Оценка логарифмической S-N кривой между 10^3 циклов (при 0.9 * Sut) и 10^6 циклов (при Se)
        s_high = 0.9 * ult_tensile_mpa
        s_low = endurance_limit_mpa
        if stress_amp_mpa >= s_high:
            cycles = 1000
        else:
            log_n = 3.0 + 3.0 * ((s_high - stress_amp_mpa) / (s_high - s_low))
            cycles = int(10 ** log_n)

        return (
            f"### Усталостный расчёт (S-N Curve):\n"
            f"- Амплитуда напряжения: **{stress_amp_mpa} МПа**\n"
            f"- Предел выносливости: {endurance_limit_mpa} МПа, предел прочности: {ult_tensile_mpa} МПа\n"
            f"- Ожидаемое число циклов до разрушения (Fatigue Life): **~{cycles:,} циклов**"
        ).replace(",", " ")

    def calc_bolt_torque(
        bolt_diameter_mm: float,
        property_class: str = "8.8",
        friction_coeff_k: float = 0.2,
    ) -> str:
        if bolt_diameter_mm <= 0:
            raise ToolError("Диаметр болта должен быть положительным")

        yield_map: dict[str, float] = {
            "4.6": 240.0,
            "5.8": 420.0,
            "8.8": 640.0,
            "10.9": 900.0,
            "12.9": 1100.0,
        }
        sy = yield_map.get(str(property_class), 640.0)

        # Приблизительная площадь сечения болта As ~ 0.78 * (D - 0.938*P)^2, упрощённо ~ 0.75 * D^2 * pi/4
        area_s = 0.7854 * (bolt_diameter_mm ** 2) * 0.75
        preload_n = round(0.75 * sy * area_s, 1)
        preload_kn = round(preload_n / 1000.0, 2)
        torque_nm = round(
            friction_coeff_k * preload_n * (bolt_diameter_mm / 1000.0), 2
        )

        return (
            f"### Инженерный расчёт усилия и момента затяжки болта M{int(bolt_diameter_mm)} (класс {property_class}):\n"
            f"- Предел текучести материала: {sy} МПа\n"
            f"- Рекомендуемое усилие предварительной затяжки (Fp): **{preload_kn} кН** ({preload_n} Н)\n"
            f"- Рекомендуемый момент затяжки (Torque, K={friction_coeff_k}): **{torque_nm} Н·м**"
        )

    def calc_rf_link_budget(
        freq_mhz: float,
        tx_power_dbm: float = 20.0,
        distance_km: float = 1.0,
        tx_gain_dbi: float = 2.15,
        rx_gain_dbi: float = 2.15,
        rx_sensitivity_dbm: float = -100.0,
    ) -> str:
        if freq_mhz <= 0 or distance_km <= 0:
            raise ToolError("Частота и дистанция должны быть положительными")

        # Потери в свободном пространстве FSPL(dB) = 32.44 + 20*log10(d_km) + 20*log10(f_MHz)
        fspl_db = round(
            32.44
            + 20.0 * math.log10(distance_km)
            + 20.0 * math.log10(freq_mhz),
            2,
        )
        rx_power_dbm = round(
            tx_power_dbm + tx_gain_dbi + rx_gain_dbi - fspl_db, 2
        )
        fade_margin_db = round(rx_power_dbm - rx_sensitivity_dbm, 2)
        is_viable = fade_margin_db >= 10.0

        return (
            f"### Расчёт бюджета радиолинии (RF Link Budget на {freq_mhz} МГц, дистанция {distance_km} км):\n"
            f"- Мощность передатчика: {tx_power_dbm} дБм, Усиление антенн: Tx={tx_gain_dbi} dBi, Rx={rx_gain_dbi} dBi\n"
            f"- Потери в свободном пространстве (FSPL): **{fspl_db} дБ**\n"
            f"- Уровень сигнала на входе приёмника (Rx Power): **{rx_power_dbm} дБм**\n"
            f"- Чувствительность приёмника: {rx_sensitivity_dbm} дБм\n"
            f"- Запас по затуханию (Fade Margin): **{fade_margin_db} дБ** -> {'✓ СВЯЗЬ НАДЁЖНА (Margin >= 10 дБ)' if is_viable else '⚠ НЕДОСТАТОЧНЫЙ ЗАПАС СВЯЗИ'}"
        )

    def calc_coaxial_cable(
        inner_diam_mm: float = 1.0,
        outer_diam_mm: float = 3.5,
        dielectric_constant: float = 2.1,
    ) -> str:
        if inner_diam_mm <= 0 or outer_diam_mm <= inner_diam_mm or dielectric_constant <= 0:
            raise ToolError("Некорректные размеры коаксиальной линии (D > d > 0)")

        # Волновое сопротивление Z0 = (138 / sqrt(er)) * log10(D / d) Ом
        z0 = round(
            (138.0 / math.sqrt(dielectric_constant))
            * math.log10(outer_diam_mm / inner_diam_mm),
            2,
        )
        # Погонная ёмкость C = 55.6 * er / ln(D/d) пФ/м
        cap_pf_m = round(
            (55.6 * dielectric_constant) / math.log(outer_diam_mm / inner_diam_mm),
            2,
        )

        return (
            f"### Расчёт коаксиального кабеля (d={inner_diam_mm} мм, D={outer_diam_mm} мм, εr={dielectric_constant}):\n"
            f"- Волновое сопротивление (Z0): **{z0} Ом**\n"
            f"- Погонная ёмкость: **{cap_pf_m} пФ/м**"
        )

    def calc_fan_cooling(
        heat_power_watts: float = 100.0,
        max_temp_rise_c: float = 10.0,
    ) -> str:
        if max_temp_rise_c <= 0:
            raise ToolError("Допустимый перепад температур должен быть положительным")

        # Требуемый объёмный расход CFM = 3.16 * Watts / delta_T_C
        cfm = round((3.16 * heat_power_watts) / max_temp_rise_c, 2)
        m3_h = round(cfm * 1.699, 2)

        return (
            f"### Тепловой и аэродинамический расчёт охлаждения:\n"
            f"- Тепловая мощность (P): {heat_power_watts} Вт, Допустимый нагрев (ΔT): {max_temp_rise_c} °C\n"
            f"- Необходимый расход вентилятора: **{cfm} CFM** (**{m3_h} м³/ч**)"
        )

    def calc_pipe_pressure_drop(
        airflow_m3_h: float = 500.0,
        pipe_diam_mm: float = 200.0,
        pipe_length_m: float = 10.0,
        friction_coeff: float = 0.02,
    ) -> str:
        if pipe_diam_mm <= 0 or pipe_length_m <= 0 or airflow_m3_h < 0:
            raise ToolError("Размеры трубы и расход должны быть положительными")

        diam_m = pipe_diam_mm / 1000.0
        area_m2 = math.pi * ((diam_m / 2.0) ** 2)
        vel_m_s = (airflow_m3_h / 3600.0) / area_m2

        # Потери давления по формуле Дарси-Вейсбаха: ΔP = f * (L/D) * (ρ v^2 / 2), ρ=1.225
        rho = 1.225
        drop_pa = round(
            friction_coeff * (pipe_length_m / diam_m) * (0.5 * rho * (vel_m_s ** 2)),
            2,
        )

        return (
            f"### Гидравлический/аэродинамический расчёт трубопровода/канала:\n"
            f"- Диаметр D={pipe_diam_mm} мм, Длина L={pipe_length_m} м, Расход {airflow_m3_h} м³/ч\n"
            f"- Скорость потока в сечении: **{round(vel_m_s, 2)} м/с**\n"
            f"- Потери давления на трение (ΔP, f={friction_coeff}): **{drop_pa} Па**"
        )

    def calc_sound_barrier(
        surface_mass_kg_m2: float = 50.0,
        freq_hz: float = 500.0,
    ) -> str:
        if surface_mass_kg_m2 <= 0 or freq_hz <= 0:
            raise ToolError("Поверхностная масса и частота должны быть положительными")

        # Закон массы (Law of Mass): TL(dB) = 20 * log10(m * f) - 47
        tl_db = round(
            20.0 * math.log10(surface_mass_kg_m2 * freq_hz) - 47.0, 1
        )
        # Оценочный индекс звукоизоляции Rw (при 500 Гц)
        rw_db = max(0.0, tl_db)

        return (
            f"### Расчёт звукоизоляции перегородки / стены (Law of Mass):\n"
            f"- Поверхностная масса стены: {surface_mass_kg_m2} кг/м², Частота: {freq_hz} Гц\n"
            f"- Затухание звуковой волны (Transmission Loss): **{rw_db} дБ**"
        )

    def calc_helmholtz_resonator(
        volume_liter: float = 10.0,
        port_diam_mm: float = 50.0,
        port_length_mm: float = 100.0,
        temp_c: float = 20.0,
    ) -> str:
        if volume_liter <= 0 or port_diam_mm <= 0 or port_length_mm <= 0:
            raise ToolError("Размеры резонатора должны быть положительными")

        # Скорость звука
        c = 331.3 * math.sqrt(1.0 + (temp_c / 273.15))
        vol_m3 = volume_liter / 1000.0
        r_m = (port_diam_mm / 1000.0) / 2.0
        area_m2 = math.pi * (r_m ** 2)
        # Эффективная длина с поправкой конца трубы L_eff = L + 0.8 * D
        l_eff = (port_length_mm / 1000.0) + 0.8 * (port_diam_mm / 1000.0)

        f0 = round(
            (c / (2.0 * math.pi)) * math.sqrt(area_m2 / (vol_m3 * l_eff)), 2
        )

        return (
            f"### Акустический расчёт резонатора Гельмгольца / фазоинвертора:\n"
            f"- Объём камеры V={volume_liter} л, Порт D={port_diam_mm} мм, L={port_length_mm} мм\n"
            f"- Собственная резонансная частота (f0 при {temp_c}°C): **{f0} Гц**"
        )

    def calc_antenna_vswr(
        forward_power_w: float = 10.0,
        reflected_power_w: float = 0.0,
        load_impedance_ohm: float = 50.0,
        line_impedance_ohm: float = 50.0,
    ) -> str:
        if forward_power_w <= 0:
            raise ToolError("Прямая мощность должна быть положительной")
        if reflected_power_w < 0 or reflected_power_w >= forward_power_w:
            raise ToolError("Отражённая мощность должна быть от 0 до Прямой мощности")

        if reflected_power_w > 0:
            gamma = math.sqrt(reflected_power_w / forward_power_w)
        else:
            gamma = abs(load_impedance_ohm - line_impedance_ohm) / (
                load_impedance_ohm + line_impedance_ohm
            )

        vswr = round((1.0 + gamma) / (1.0 - gamma), 2) if gamma < 0.999 else 999.0
        return_loss_db = round(-20.0 * math.log10(max(1e-5, gamma)), 2)
        refl_pct = round((gamma ** 2) * 100.0, 2)
        trans_pct = round(100.0 - refl_pct, 2)

        return (
            f"### Расчёт согласования антенны (VSWR / КСВ и Return Loss):\n"
            f"- Коэффициент стоячей волны (VSWR / КСВ): **{vswr}:1** -> {'✓ ОТЛИЧНОЕ СОГЛАСОВАНИЕ (< 1.5:1)' if vswr <= 1.5 else '✓ ДОПУСТИМОЕ (< 2.0:1)' if vswr <= 2.0 else '⚠ ПЛОХОЕ СОГЛАСОВАНИЕ (VSWR > 2.0:1)'}\n"
            f"- Возвратные потери (Return Loss): **{return_loss_db} дБ** (Коэффициент отражения Γ = {round(gamma, 4)})\n"
            f"- Эффективность передачи: **{trans_pct}%** мощности излучается, {refl_pct}% отражается"
        )

    def calc_antenna_matching_network(
        freq_mhz: float,
        antenna_r_ohm: float,
        antenna_x_ohm: float = 0.0,
        target_z0_ohm: float = 50.0,
    ) -> str:
        if freq_mhz <= 0 or antenna_r_ohm <= 0 or target_z0_ohm <= 0:
            raise ToolError("Частота и сопротивления должны быть положительными")

        omega = 2.0 * math.pi * freq_mhz * 1e6
        r_l = antenna_r_ohm
        z0 = target_z0_ohm

        if r_l < z0:
            q = math.sqrt((z0 / r_l) - 1.0)
            x_s = q * r_l - antenna_x_ohm
            x_p = z0 / q
        else:
            q = math.sqrt((r_l / z0) - 1.0)
            x_p = r_l / q
            x_s = q * z0 - antenna_x_ohm

        l_nh = round((abs(x_s) / omega) * 1e9, 2)
        c_pf = round((1.0 / (omega * abs(x_p))) * 1e12, 2)

        return (
            f"### Инженерный расчёт согласующей цепи (L-Network Matching на {freq_mhz} МГц):\n"
            f"- Импеданс антенны: Z_ant = {r_l} + j({antenna_x_ohm}) Ом -> Целевой импеданс Z0 = {z0} Ом\n"
            f"- Добротность цепи Q = {round(q, 2)}\n"
            f"- Рекомендуемые элементы согласующего Г-звена (L-Network):\n"
            f"  * Индуктивность: **L = {l_nh} нГн** ({'последовательно с антенной' if r_l < z0 else 'параллельно антенне'})\n"
            f"  * Ёмкость: **C = {c_pf} пФ** ({'параллельно генератору' if r_l < z0 else 'последовательно с линией'})"
        )

    def calc_yagi_uda_antenna(
        freq_mhz: float,
        elements_count: int = 3,
        element_diam_mm: float = 4.0,
    ) -> str:
        if freq_mhz <= 0 or elements_count < 2:
            raise ToolError("Частота должна быть положительной, число элементов Яги >= 2")

        c = 299.792458
        wl_m = round(c / freq_mhz, 4)
        wl_mm = round(wl_m * 1000.0, 2)

        ref_mm = round(wl_mm * 0.495, 1)
        driven_mm = round(wl_mm * 0.473, 1)
        dir_mm = round(wl_mm * 0.440, 1)
        spacing_mm = round(wl_mm * 0.20, 1)
        boom_mm = round(spacing_mm * (elements_count - 1), 1)

        est_gain_dbi = round(7.0 + 1.2 * math.sqrt(elements_count - 2), 2) if elements_count > 2 else 5.5
        ftb_db = round(12.0 + 2.0 * math.sqrt(elements_count - 1), 1)

        res = [
            f"### Полный инженерный расчёт направленной антенны Уда-Яги (Yagi-Uda) на {freq_mhz} МГц:",
            f"- Длина волны λ: **{wl_m} м** ({wl_mm} мм), Диаметр элементов: {element_diam_mm} мм",
            f"- Расчётный коэффициент усиления: **~{est_gain_dbi} dBi** (F/B Ratio: {ftb_db} дБ)",
            f"- Общая длина бума/траверсы: **{boom_mm} мм** (шаг элементов {spacing_mm} мм)",
            f"- Таблица размеров элементов антенны:",
            f"  1. Рефлектор: **{ref_mm} мм** (0.495λ)",
            f"  2. Активный вибратор (Driven): **{driven_mm} мм** (0.473λ)",
        ]
        for idx in range(3, elements_count + 1):
            d_len = round(dir_mm - (idx - 3) * (wl_mm * 0.008), 1)
            res.append(f"  {idx}. Директор #{idx-2}: **{d_len} мм**")
        return "\n".join(res)

    def calc_patch_antenna(
        freq_mhz: float,
        dielectric_constant: float = 4.4,
        substrate_height_mm: float = 1.6,
    ) -> str:
        if freq_mhz <= 0 or dielectric_constant <= 1.0 or substrate_height_mm <= 0:
            raise ToolError("Некорректные параметры субстрата патч-антенны")

        c = 299792458.0
        f_hz = freq_mhz * 1e6
        h_m = substrate_height_mm / 1000.0

        # Ширина патча W
        w_m = (c / (2.0 * f_hz)) * math.sqrt(2.0 / (dielectric_constant + 1.0))
        w_mm = round(w_m * 1000.0, 2)

        # Эффективная диэлектрическая проницаемость e_reff
        e_reff = round(
            0.5 * (dielectric_constant + 1.0)
            + 0.5 * (dielectric_constant - 1.0) * ((1.0 + 12.0 * (h_m / w_m)) ** -0.5),
            3,
        )

        # Прирост длины за счёт краевого эффекта delta_L
        delta_l = h_m * 0.412 * ((e_reff + 0.3) * (w_m / h_m + 0.264)) / (
            (e_reff - 0.258) * (w_m / h_m + 0.8)
        )
        l_mm = round(((c / (2.0 * f_hz * math.sqrt(e_reff))) - 2.0 * delta_l) * 1000.0, 2)

        return (
            f"### Инженерный расчёт микрополосковой патч-антенны (PCB Patch Antenna на {freq_mhz} МГц):\n"
            f"- Субстрат PCB: εr = {dielectric_constant} (FR4/Rogers), толщина h = {substrate_height_mm} мм\n"
            f"- Эффективная проницаемость ε_reff: {e_reff}\n"
            f"- Размеры излучающего прямоугольника:\n"
            f"  * Ширина патча (W): **{w_mm} мм**\n"
            f"  * Длина патча (L): **{l_mm} мм**\n"
            f"- Расчётный коэффициент усиления: **~6.5 dBi**, направленность в верхнюю полусферу"
        )

    def calc_propeller_thrust_power(
        diameter_mm: float,
        rpm: float,
        pitch_mm: float,
        blades_count: int = 2,
        air_density: float = 1.225,
    ) -> str:
        if diameter_mm <= 0 or rpm <= 0 or pitch_mm <= 0 or blades_count < 2:
            raise ToolError("Некорректные параметры пропеллера / крыльчатки")

        diam_m = diameter_mm / 1000.0
        n_rps = rpm / 60.0
        pitch_m = pitch_mm / 1000.0

        # Оценочные аэродинамические коэффициенты винта (BEMT approximation)
        p_d_ratio = pitch_m / diam_m
        c_t = round(max(0.05, 0.1 * p_d_ratio * (blades_count / 2.0)), 4)
        c_p = round(max(0.02, 0.05 * (p_d_ratio ** 1.5) * (blades_count / 2.0)), 4)

        # Тяга T = Ct * rho * n^2 * D^4
        thrust_n = round(c_t * air_density * (n_rps ** 2) * (diam_m ** 4), 2)
        thrust_gf = round(thrust_n * 101.97, 1)

        # Мощность P = Cp * rho * n^3 * D^5
        power_w = round(c_p * air_density * (n_rps ** 3) * (diam_m ** 5), 2)
        torque_nm = round(power_w / (2.0 * math.pi * n_rps), 3)

        tip_speed = round(math.pi * diam_m * n_rps, 2)
        mach = round(tip_speed / 343.0, 3)

        return (
            f"### Аэродинамический расчёт пропеллера / крыльчатки (D={diameter_mm} мм, {rpm} RPM, {blades_count} лопасти):\n"
            f"- Скорость кончика лопасти (Tip Speed): **{tip_speed} м/с** (Mach **{mach}**)\n"
            f"- Аэродинамические коэффициенты: Ct = {c_t}, Cp = {c_p} (Шаг/Диаметр P/D = {round(p_d_ratio, 2)})\n"
            f"- Расчётная тяга (Thrust): **{thrust_n} Н** ({thrust_gf} гс)\n"
            f"- Механическая мощность на валу: **{power_w} Вт** (Крутящий момент: {torque_nm} Н·м)"
        )

    def calc_propeller_noise(
        diameter_mm: float,
        rpm: float,
        blades_count: int = 3,
        observer_distance_m: float = 1.0,
    ) -> str:
        if diameter_mm <= 0 or rpm <= 0 or observer_distance_m <= 0:
            raise ToolError("Некорректные размеры пропеллера и дистанция")

        diam_m = diameter_mm / 1000.0
        n_rps = rpm / 60.0
        tip_speed = round(math.pi * diam_m * n_rps, 2)
        mach = round(tip_speed / 343.0, 3)
        bpf = round(n_rps * blades_count, 1)

        # Оценка аэродинамического вихревого шума SPL (Gutin-Woods approximation)
        # SPL ~ 50 + 50*log10(Mach) + 10*log10(Blades) - 20*log10(distance)
        base_spl = 75.0 + 50.0 * math.log10(max(0.05, mach)) + 10.0 * math.log10(blades_count)
        spl_db = round(base_spl - 20.0 * math.log10(observer_distance_m), 1)

        is_low_noise = mach <= 0.35 and spl_db <= 65.0

        return (
            f"### Акустический расчёт шума пропеллера / вентилятора (D={diameter_mm} мм, {rpm} RPM):\n"
            f"- Скорость кончиков лопастей: **{tip_speed} м/с** (Число Маха $M_{{tip}} = {mach}$)\n"
            f"- Частота следования лопастей (Blade Pass Frequency, BPF): **{bpf} Гц** (основная гармоника шума)\n"
            f"- Оценочный уровень шума на расстоянии {observer_distance_m} м: **{spl_db} дБА**\n"
            f"- Акустический статус: {'✓ МАЛОШУМНЫЙ ПРОПЕЛЛЕР (Mach <= 0.35)' if is_low_noise else '⚠ ПОВЫШЕННЫЙ ШУМ (рекомендуется снизить обороты или увеличить число лопастей)'}\n"
            f"- Рекомендации для снижения шума:\n"
            f"  1. Использовать саблевидную форму лопасти (Sickle-swept blade) для снижения концевых вихрей.\n"
            f"  2. Увеличить число лопастей до {blades_count+2} при одновременном снижении RPM.\n"
            f"  3. Применить закон крутки с разгрузкой законцовки лопасти."
        )

    def calc_low_noise_blade_geometry(
        diameter_mm: float,
        hub_diameter_mm: float = 30.0,
        design_rpm: float = 3000.0,
        design_velocity_m_s: float = 10.0,
    ) -> str:
        if diameter_mm <= hub_diameter_mm or design_rpm <= 0:
            raise ToolError("Некорректные параметры геометрии лопасти")

        r_tip = diameter_mm / 2.0
        r_hub = hub_diameter_mm / 2.0
        omega = (design_rpm * 2.0 * math.pi) / 60.0

        res = [
            f"### Расчёт профиля и крутки малошумной лопасти (D={diameter_mm} мм, {design_rpm} RPM, v={design_velocity_m_s} м/с):",
            f"- Оптимальное радиальное распределение углов установки $\\theta(r)$ и хорды $c(r)$:",
        ]

        # Расчёт для 25%, 50%, 75% и 100% радиуса (BEMT Betz distribution)
        for pct in (25, 50, 75, 100):
            r_mm = round(r_hub + (r_tip - r_hub) * (pct / 100.0), 1)
            r_m = r_mm / 1000.0
            phi_rad = math.atan2(design_velocity_m_s, omega * r_m)
            # Угол атаки alpha=4° для оптимального аэродинамического качества L/D
            theta_deg = round(math.degrees(phi_rad) + 4.0, 1)
            chord_mm = round((40.0 * r_tip / r_mm) * math.sin(phi_rad), 1)
            res.append(
                f"  * **{pct}% R** (r = {r_mm} мм): Угол крутки **θ = {theta_deg}°**, Хорда **c = {chord_mm} мм**"
            )

        res.append("- Аэродинамический профиль: рекомендуется CLARK-Y или NACA 4412 с плавным саблевидным изгибом.")
        return "\n".join(res)

    return [
        Tool(
            name="physics.calc_strength",
            description="Рассчитать механическое напряжение σ, запас прочности по пределу текучести и максимальный прогиб балок.",
            parameters={
                "type": "object",
                "properties": {
                    "load_n": {"type": "number", "description": "Нагрузка в Н"},
                    "area_mm2": {"type": "number", "description": "Площадь сечения (мм²)"},
                    "yield_strength_mpa": {"type": "number", "description": "Предел текучести (МПа)"},
                    "beam_length_mm": {"type": "number", "description": "Длина балки (мм)"},
                    "modulus_gpa": {"type": "number", "description": "Модуль Юнга (ГПа)"},
                    "inertia_mm4": {"type": "number", "description": "Момент инерции I (мм⁴)"},
                },
                "required": ["load_n", "area_mm2"],
            },
            fn=calc_strength,
            skills=["physics", "strength", "mechanics", "engineering", "math", "cad"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "strength_calc",
                "speed": "fast",
                "tags": ["physics", "strength", "stress", "safety_factor", "mechanics", "beam"],
            },
            example='physics.calc_strength(load_n=10000.0, area_mm2=50.0, yield_strength_mpa=250.0)',
        ),
        Tool(
            name="physics.calc_em_field",
            description="Рассчитать электромагнитное поле (магнитную индукцию B в мТл) прямого проводника с током и соленоида.",
            parameters={
                "type": "object",
                "properties": {
                    "current_a": {"type": "number", "description": "Сила тока (А)"},
                    "distance_mm": {"type": "number", "description": "Расстояние от провода (мм)"},
                    "turns_count": {"type": "integer", "description": "Число витков N"},
                    "solenoid_length_mm": {"type": "number", "description": "Длина соленоида (мм)"},
                    "core_permeability": {"type": "number", "description": "Магнитная проницаемость μ"},
                },
                "required": ["current_a"],
            },
            fn=calc_em_field,
            skills=["physics", "electromagnetics", "em_field", "engineering", "math"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "em_calc",
                "speed": "fast",
                "tags": ["physics", "electromagnetic", "magnetic", "field", "solenoid", "current"],
            },
            example='physics.calc_em_field(current_a=5.0, turns_count=100, solenoid_length_mm=50.0)',
        ),
        Tool(
            name="physics.calc_antenna",
            description="Рассчитать геометрию антенны (полуволновой диполь λ/2, штырь λ/4) и длину волны по частоте в МГц.",
            parameters={
                "type": "object",
                "properties": {
                    "freq_mhz": {"type": "number", "description": "Частота в МГц"},
                    "antenna_type": {"type": "string", "description": "dipole (λ/2) или monopole (λ/4)"},
                    "velocity_factor": {"type": "number", "description": "Коэффициент укорочения (0.95)"},
                },
                "required": ["freq_mhz"],
            },
            fn=calc_antenna,
            skills=["physics", "antennas", "rf", "electromagnetics", "engineering", "math"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "antenna_calc",
                "speed": "fast",
                "tags": ["physics", "antenna", "rf", "radio", "dipole", "wavelength"],
            },
            example='physics.calc_antenna(freq_mhz=433.92, antenna_type="dipole")',
        ),
        Tool(
            name="physics.calc_airflow",
            description="Рассчитать аэродинамические параметры воздушного потока: число Рейнольдса (Re), режим течения и силу лобового сопротивления.",
            parameters={
                "type": "object",
                "properties": {
                    "velocity_m_s": {"type": "number", "description": "Скорость потока (м/с)"},
                    "char_length_m": {"type": "number", "description": "Характерный размер L (м)"},
                    "kinematic_viscosity": {"type": "number", "description": "Кинематическая вязкость ν"},
                    "drag_coeff": {"type": "number", "description": "Коэффициент сопротивления Cd"},
                    "frontal_area_m2": {"type": "number", "description": "Лобовая площадь (м²)"},
                },
                "required": ["velocity_m_s"],
            },
            fn=calc_airflow,
            skills=["physics", "airflow", "aerodynamics", "fluid_dynamics", "engineering", "math"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "airflow_calc",
                "speed": "fast",
                "tags": ["physics", "airflow", "aerodynamics", "reynolds", "drag", "fluid"],
            },
            example='physics.calc_airflow(velocity_m_s=20.0, char_length_m=0.5, drag_coeff=0.3, frontal_area_m2=2.0)',
        ),
        Tool(
            name="physics.calc_acoustics",
            description="Рассчитать параметры звуковой волны / акустики: скорость звука, длину волны λ, уровень звукового давления SPL (дБ) и резонансы труб.",
            parameters={
                "type": "object",
                "properties": {
                    "freq_hz": {"type": "number", "description": "Частота в Гц"},
                    "medium": {"type": "string", "description": "Среда: air, water, steel"},
                    "temperature_c": {"type": "number", "description": "Температура (°C)"},
                    "pressure_pa": {"type": "number", "description": "Звуковое давление в Па"},
                    "pipe_length_m": {"type": "number", "description": "Длина трубы (м)"},
                    "pipe_closed_end": {"type": "boolean", "description": "Труба закрыта с 1 конца"},
                },
                "required": ["freq_hz"],
            },
            fn=calc_acoustics,
            skills=["physics", "acoustics", "sound", "waves", "engineering", "math"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "acoustics_calc",
                "speed": "fast",
                "tags": ["physics", "acoustics", "sound", "wavelength", "decibel", "resonance"],
            },
            example='physics.calc_acoustics(freq_hz=440.0, medium="air", temperature_c=20.0, pressure_pa=0.2)',
        ),
        Tool(
            name="physics.calc_fatigue_life",
            description="Рассчитать усталостную долговечность детали (S-N Curve / Fatigue Life) при циклической знакопеременной нагрузке.",
            parameters={
                "type": "object",
                "properties": {
                    "stress_amp_mpa": {"type": "number", "description": "Амплитуда напряжения (МПа)"},
                    "endurance_limit_mpa": {"type": "number", "description": "Предел выносливости (МПа)"},
                    "ult_tensile_mpa": {"type": "number", "description": "Предел прочности (МПа)"},
                },
                "required": ["stress_amp_mpa"],
            },
            fn=calc_fatigue_life,
            skills=["physics", "strength", "fatigue", "mechanics", "engineering", "math"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "fatigue_calc",
                "speed": "fast",
                "tags": ["physics", "fatigue", "stress", "strength", "mechanics", "cycles"],
            },
            example='physics.calc_fatigue_life(stress_amp_mpa=200.0, endurance_limit_mpa=150.0)',
        ),
        Tool(
            name="physics.calc_bolt_torque",
            description="Рассчитать усилие предварительной затяжки и рекомендуемый момент затяжки болта М4–М12 (класс 8.8 / 10.9).",
            parameters={
                "type": "object",
                "properties": {
                    "bolt_diameter_mm": {"type": "number", "description": "Диаметр резьбы болта (мм)"},
                    "property_class": {"type": "string", "description": "Класс прочности (4.6, 8.8, 10.9)"},
                    "friction_coeff_k": {"type": "number", "description": "Коэффициент трения (0.2)"},
                },
                "required": ["bolt_diameter_mm"],
            },
            fn=calc_bolt_torque,
            skills=["physics", "strength", "bolt", "torque", "engineering", "mechanics"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "bolt_calc",
                "speed": "fast",
                "tags": ["physics", "bolt", "torque", "preload", "strength", "mechanics"],
            },
            example='physics.calc_bolt_torque(bolt_diameter_mm=8.0, property_class="8.8")',
        ),
        Tool(
            name="physics.calc_rf_link_budget",
            description="Рассчитать бюджет радиолинии (RF Link Budget): потери FSPL, уровень приёма Rx дБм и запас по затуханию (Fade Margin).",
            parameters={
                "type": "object",
                "properties": {
                    "freq_mhz": {"type": "number", "description": "Частота в МГц"},
                    "tx_power_dbm": {"type": "number", "description": "Мощность передатчика в дБм"},
                    "distance_km": {"type": "number", "description": "Дистанция в км"},
                    "tx_gain_dbi": {"type": "number", "description": "Усиление антенны Tx (dBi)"},
                    "rx_gain_dbi": {"type": "number", "description": "Усиление антенны Rx (dBi)"},
                    "rx_sensitivity_dbm": {"type": "number", "description": "Чувствительность Rx (дБм)"},
                },
                "required": ["freq_mhz", "distance_km"],
            },
            fn=calc_rf_link_budget,
            skills=["physics", "rf", "antennas", "electromagnetics", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "rf_budget",
                "speed": "fast",
                "tags": ["physics", "rf", "radio", "link_budget", "fspl", "antenna"],
            },
            example='physics.calc_rf_link_budget(freq_mhz=868.0, tx_power_dbm=14.0, distance_km=2.0)',
        ),
        Tool(
            name="physics.calc_coaxial_cable",
            description="Рассчитать волновое сопротивление Z0 (Ом) и погонную ёмкость коаксиального кабеля / линии.",
            parameters={
                "type": "object",
                "properties": {
                    "inner_diam_mm": {"type": "number", "description": "Диаметр центральной жилы d (мм)"},
                    "outer_diam_mm": {"type": "number", "description": "Внутренний диаметр экрана D (мм)"},
                    "dielectric_constant": {"type": "number", "description": "Диэлектрическая проницаемость εr"},
                },
            },
            fn=calc_coaxial_cable,
            skills=["physics", "rf", "electromagnetics", "engineering", "cable"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "coax_calc",
                "speed": "fast",
                "tags": ["physics", "coaxial", "impedance", "rf", "cable"],
            },
            example='physics.calc_coaxial_cable(inner_diam_mm=1.0, outer_diam_mm=3.5, dielectric_constant=2.1)',
        ),
        Tool(
            name="physics.calc_fan_cooling",
            description="Рассчитать необходимый расход вентилятора (CFM / м³/ч) для воздушного охлаждения при заданной тепловой мощности и ΔT.",
            parameters={
                "type": "object",
                "properties": {
                    "heat_power_watts": {"type": "number", "description": "Тепловая мощность (Вт)"},
                    "max_temp_rise_c": {"type": "number", "description": "Допустимый нагрев ΔT (°C)"},
                },
            },
            fn=calc_fan_cooling,
            skills=["physics", "airflow", "cooling", "hvac", "engineering", "thermal"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "cooling_calc",
                "speed": "fast",
                "tags": ["physics", "airflow", "cooling", "fan", "thermal", "hvac"],
            },
            example='physics.calc_fan_cooling(heat_power_watts=150.0, max_temp_rise_c=15.0)',
        ),
        Tool(
            name="physics.calc_pipe_pressure_drop",
            description="Рассчитать гидравлическое сопротивление / потери давления (Па) воздушного потока в трубе или вентиляционном канале.",
            parameters={
                "type": "object",
                "properties": {
                    "airflow_m3_h": {"type": "number", "description": "Расход воздуха (м³/ч)"},
                    "pipe_diam_mm": {"type": "number", "description": "Диаметр трубы/канала (мм)"},
                    "pipe_length_m": {"type": "number", "description": "Длина канала (м)"},
                    "friction_coeff": {"type": "number", "description": "Коэффициент трения f (0.02)"},
                },
            },
            fn=calc_pipe_pressure_drop,
            skills=["physics", "airflow", "hvac", "aerodynamics", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "pressure_calc",
                "speed": "fast",
                "tags": ["physics", "airflow", "pressure_drop", "pipe", "hvac", "aerodynamics"],
            },
            example='physics.calc_pipe_pressure_drop(airflow_m3_h=500.0, pipe_diam_mm=200.0, pipe_length_m=10.0)',
        ),
        Tool(
            name="physics.calc_sound_barrier",
            description="Рассчитать звукоизоляцию перегородки / стены (индекс звукоизоляции Rw в дБ) по закону массы (Law of Mass).",
            parameters={
                "type": "object",
                "properties": {
                    "surface_mass_kg_m2": {"type": "number", "description": "Поверхностная масса стены (кг/м²)"},
                    "freq_hz": {"type": "number", "description": "Частота звуковой волны (Гц)"},
                },
            },
            fn=calc_sound_barrier,
            skills=["physics", "acoustics", "sound", "insulation", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "sound_insulation",
                "speed": "fast",
                "tags": ["physics", "acoustics", "sound", "insulation", "decibel", "barrier"],
            },
            example='physics.calc_sound_barrier(surface_mass_kg_m2=50.0, freq_hz=500.0)',
        ),
        Tool(
            name="physics.calc_helmholtz_resonator",
            description="Рассчитать собственную резонансную частоту f0 акустического резонатора Гельмгольца / фазоинвертора.",
            parameters={
                "type": "object",
                "properties": {
                    "volume_liter": {"type": "number", "description": "Объём камеры (л)"},
                    "port_diam_mm": {"type": "number", "description": "Диаметр порта (мм)"},
                    "port_length_mm": {"type": "number", "description": "Длина порта (мм)"},
                    "temp_c": {"type": "number", "description": "Температура воздуха (°C)"},
                },
            },
            fn=calc_helmholtz_resonator,
            skills=["physics", "acoustics", "sound", "resonance", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "helmholtz_calc",
                "speed": "fast",
                "tags": ["physics", "acoustics", "helmholtz", "resonator", "sound", "frequency"],
            },
            example='physics.calc_helmholtz_resonator(volume_liter=15.0, port_diam_mm=50.0, port_length_mm=100.0)',
        ),
        Tool(
            name="physics.calc_antenna_vswr",
            description="Рассчитать коэффициент стоячей волны (КСВ / VSWR), возвратные потери (Return Loss дБ) и эффективность согласования антенны.",
            parameters={
                "type": "object",
                "properties": {
                    "forward_power_w": {"type": "number", "description": "Прямая мощность (Вт)"},
                    "reflected_power_w": {"type": "number", "description": "Отражённая мощность (Вт)"},
                    "load_impedance_ohm": {"type": "number", "description": "Импеданс антенны Z_L (Ом)"},
                    "line_impedance_ohm": {"type": "number", "description": "Волновое сопротивление линии Z_0 (Ом)"},
                },
            },
            fn=calc_antenna_vswr,
            skills=["physics", "antennas", "rf", "electromagnetics", "vswr", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "antenna_calc",
                "speed": "fast",
                "tags": ["physics", "antenna", "rf", "vswr", "return_loss", "matching"],
            },
            example='physics.calc_antenna_vswr(forward_power_w=10.0, reflected_power_w=0.5)',
        ),
        Tool(
            name="physics.calc_antenna_matching_network",
            description="Рассчитать элементы согласующей LC-цепи (L-Network Matching: L нГн, C пФ) для согласования импеданса антенны с линией 50 Ом.",
            parameters={
                "type": "object",
                "properties": {
                    "freq_mhz": {"type": "number", "description": "Рабочая частота (МГц)"},
                    "antenna_r_ohm": {"type": "number", "description": "Акт. сопротивление антенны (Ом)"},
                    "antenna_x_ohm": {"type": "number", "description": "Реактивное сопротивление (Ом)"},
                    "target_z0_ohm": {"type": "number", "description": "Целевое сопротивление линии (50 Ом)"},
                },
                "required": ["freq_mhz", "antenna_r_ohm"],
            },
            fn=calc_antenna_matching_network,
            skills=["physics", "antennas", "rf", "electromagnetics", "matching", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "antenna_calc",
                "speed": "fast",
                "tags": ["physics", "antenna", "rf", "matching", "l_network", "lc"],
            },
            example='physics.calc_antenna_matching_network(freq_mhz=433.92, antenna_r_ohm=25.0)',
        ),
        Tool(
            name="physics.calc_yagi_uda_antenna",
            description="Рассчитать размеры элементов (рефлектор, вибратор, директоры) и усиление dBi направленной антенны Уда-Яги (Yagi-Uda).",
            parameters={
                "type": "object",
                "properties": {
                    "freq_mhz": {"type": "number", "description": "Рабочая частота (МГц)"},
                    "elements_count": {"type": "integer", "description": "Число элементов антенны (>= 2)"},
                    "element_diam_mm": {"type": "number", "description": "Диаметр трубки элемента (мм)"},
                },
                "required": ["freq_mhz"],
            },
            fn=calc_yagi_uda_antenna,
            skills=["physics", "antennas", "rf", "electromagnetics", "yagi", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "antenna_calc",
                "speed": "fast",
                "tags": ["physics", "antenna", "rf", "yagi", "directional", "gain"],
            },
            example='physics.calc_yagi_uda_antenna(freq_mhz=433.92, elements_count=5)',
        ),
        Tool(
            name="physics.calc_patch_antenna",
            description="Рассчитать размеры W и L печатной микрополосковой патч-антенны (PCB Patch Antenna) на субстрате FR4/Rogers.",
            parameters={
                "type": "object",
                "properties": {
                    "freq_mhz": {"type": "number", "description": "Частота в МГц"},
                    "dielectric_constant": {"type": "number", "description": "Диэлектрическая проницаемость εr"},
                    "substrate_height_mm": {"type": "number", "description": "Толщина субстрата h (мм)"},
                },
                "required": ["freq_mhz"],
            },
            fn=calc_patch_antenna,
            skills=["physics", "antennas", "rf", "electromagnetics", "pcb", "engineering", "patch_antenna"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "antenna_calc",
                "speed": "fast",
                "tags": ["physics", "antenna", "rf", "patch", "patch_antenna", "pcb", "microstrip", "pcb_antenna"],
            },
            example='physics.calc_patch_antenna(freq_mhz=2400.0, dielectric_constant=4.4)',
        ),
        Tool(
            name="physics.calc_propeller_thrust_power",
            description="Рассчитать аэродинамическую тягу (Н), крутящий момент, мощность (Вт) и скорость кончика лопасти пропеллера / вентилятора.",
            parameters={
                "type": "object",
                "properties": {
                    "diameter_mm": {"type": "number", "description": "Диаметр винта (мм)"},
                    "rpm": {"type": "number", "description": "Обороты в минуту (RPM)"},
                    "pitch_mm": {"type": "number", "description": "Геометрический шаг (мм)"},
                    "blades_count": {"type": "integer", "description": "Число лопастей"},
                },
                "required": ["diameter_mm", "rpm", "pitch_mm"],
            },
            fn=calc_propeller_thrust_power,
            skills=["physics", "propeller", "fan", "aerodynamics", "thrust", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "propeller_calc",
                "speed": "fast",
                "tags": ["physics", "propeller", "fan", "thrust", "power", "aerodynamics"],
            },
            example='physics.calc_propeller_thrust_power(diameter_mm=250.0, rpm=3000.0, pitch_mm=120.0)',
        ),
        Tool(
            name="physics.calc_propeller_noise",
            description="Рассчитать акустический шум пропеллера / вентилятора (дБА), число Маха кончика лопасти и получить рекомендации по снижению шума.",
            parameters={
                "type": "object",
                "properties": {
                    "diameter_mm": {"type": "number", "description": "Диаметр винта (мм)"},
                    "rpm": {"type": "number", "description": "Обороты в минуту (RPM)"},
                    "blades_count": {"type": "integer", "description": "Число лопастей"},
                    "observer_distance_m": {"type": "number", "description": "Расстояние до наблюдателя (м)"},
                },
                "required": ["diameter_mm", "rpm"],
            },
            fn=calc_propeller_noise,
            skills=["physics", "propeller", "fan", "acoustics", "noise", "aerodynamics", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "propeller_noise_calc",
                "speed": "fast",
                "tags": ["physics", "propeller", "fan", "noise", "acoustics", "spl", "aerodynamics"],
            },
            example='physics.calc_propeller_noise(diameter_mm=200.0, rpm=2500.0, blades_count=5)',
        ),
        Tool(
            name="physics.calc_low_noise_blade_geometry",
            description="Рассчитать профиль и закон крутки θ(r) малошумной лопасти пропеллера по радиусу (25%, 50%, 75%, 100% R).",
            parameters={
                "type": "object",
                "properties": {
                    "diameter_mm": {"type": "number", "description": "Диаметр винта (мм)"},
                    "hub_diameter_mm": {"type": "number", "description": "Диаметр втулки (мм)"},
                    "design_rpm": {"type": "number", "description": "Расчётные обороты (RPM)"},
                    "design_velocity_m_s": {"type": "number", "description": "Расчётная скорость набегающего потока (м/с)"},
                },
                "required": ["diameter_mm"],
            },
            fn=calc_low_noise_blade_geometry,
            skills=["physics", "propeller", "fan", "aerodynamics", "blade", "engineering"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "blade_geometry_calc",
                "speed": "fast",
                "tags": ["physics", "propeller", "blade", "geometry", "twist", "aerodynamics"],
            },
            example='physics.calc_low_noise_blade_geometry(diameter_mm=250.0, design_rpm=2500.0)',
        ),
    ]
