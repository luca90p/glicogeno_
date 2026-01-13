import streamlit as st
import pandas as pd
import numpy as np
import altair as alt
from dataclasses import dataclass
from enum import Enum
import math
import xml.etree.ElementTree as ET
import io 

# --- 0. SISTEMA DI PROTEZIONE (LOGIN) ---
def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["password"] == "glicogeno2025": 
            st.session_state["password_correct"] = True
            del st.session_state["password"]  
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input(
            "🔐 Inserisci la Password per accedere al Simulatore", type="password", on_change=password_entered, key="password"
        )
        return False
    elif not st.session_state["password_correct"]:
        st.text_input(
            "🔐 Inserisci la Password per accedere al Simulatore", type="password", on_change=password_entered, key="password"
        )
        st.error("😕 Password errata. Riprova.")
        return False
    else:
        return True

if not check_password():
    st.stop()

# --- 1. PARAMETRI FISIOLOGICI ---

class Sex(Enum):
    MALE = "Uomo"
    FEMALE = "Donna"

class TrainingStatus(Enum):
    SEDENTARY = (13.0, "Sedentario / Principiante")
    RECREATIONAL = (16.0, "Attivo / Amatore")
    TRAINED = (19.0, "Allenato (Intermedio)")
    ADVANCED = (22.0, "Avanzato / Competitivo")
    ELITE = (25.0, "Elite / Pro")

    def __init__(self, val, label):
        self.val = val
        self.label = label

class SportType(Enum):
    CYCLING = (0.63, "Ciclismo (Prevalenza arti inferiori)")
    RUNNING = (0.75, "Corsa (Arti inferiori + Core)")
    TRIATHLON = (0.85, "Triathlon (Multidisciplinare)")
    XC_SKIING = (0.95, "Sci di Fondo (Whole Body)")
    SWIMMING = (0.80, "Nuoto (Arti sup. + inf.)")

    def __init__(self, val, label):
        self.val = val
        self.label = label

# --- PARAMETRI STATO FISIOLOGICO ---
class DietType(Enum):
    HIGH_CARB = (1.25, "Carico Carboidrati (Supercompensazione)", 8.0)
    NORMAL = (1.00, "Regime Normocalorico Misto (Baseline)", 5.0)
    LOW_CARB = (0.50, "Restrizione Glucidica / Low Carb", 2.5)

    def __init__(self, factor, label, ref_value):
        self.factor = factor
        self.label = label
        self.ref_value = ref_value

class FatigueState(Enum):
    RESTED = (1.0, "Riposo / Tapering (Pieno Recupero)")
    ACTIVE = (0.9, "Carico di lavoro moderato (24h prec.)")
    TIRED = (0.60, "Alto carico o Danno Muscolare (EIMD)")

    def __init__(self, factor, label):
        self.factor = factor
        self.label = label

class SleepQuality(Enum):
    GOOD = (1.0, "Ottimale (>7h, ristoratore)")
    AVERAGE = (0.95, "Sufficiente (6-7h)")
    POOR = (0.85, "Insufficiente / Disturbato (<6h)")

    def __init__(self, factor, label):
        self.factor = factor
        self.label = label

class MenstrualPhase(Enum):
    NONE = (1.0, "Non applicabile")
    FOLLICULAR = (1.0, "Fase Follicolare")
    LUTEAL = (0.95, "Fase Luteale (Premestruale)")

    def __init__(self, factor, label):
        self.factor = factor
        self.label = label

class ChoMixType(Enum):
    GLUCOSE_ONLY = (1.0, 60.0, "Solo Glucosio/Maltodestrine (Standard)")
    MIX_2_1 = (1.5, 90.0, "Mix 2:1 (Maltodestrine:Fruttosio)")
    MIX_1_08 = (1.7, 105.0, "Mix 1:0.8 (High Frructose)")

    def __init__(self, ox_factor, max_rate_gh, label):
        self.ox_factor = ox_factor 
        self.max_rate_gh = max_rate_gh 
        self.label = label

@dataclass
class Subject:
    weight_kg: float
    height_cm: float 
    body_fat_pct: float
    sex: Sex
    glycogen_conc_g_kg: float
    sport: SportType
    liver_glycogen_g: float = 100.0
    filling_factor: float = 1.0 
    uses_creatine: bool = False
    menstrual_phase: MenstrualPhase = MenstrualPhase.NONE
    glucose_mg_dl: float = None
    vo2max_absolute_l_min: float = 3.5 
    muscle_mass_kg: float = None 

    @property
    def lean_body_mass(self) -> float:
        return self.weight_kg * (1.0 - self.body_fat_pct)

    @property
    def muscle_fraction(self) -> float:
        base = 0.50 if self.sex == Sex.MALE else 0.42
        if self.glycogen_conc_g_kg >= 22.0:
            base += 0.03
        return base

# --- 2. LOGICA DI CALCOLO ---

def get_concentration_from_vo2max(vo2_max):
    conc = 13.0 + (vo2_max - 30.0) * 0.24
    if conc < 12.0: conc = 12.0
    if conc > 26.0: conc = 26.0
    return conc

def calculate_depletion_factor(steps, activity_min, s_fatigue):
    steps_base = 10000 
    steps_factor = (steps - steps_base) / 5000 * 0.1 * 0.4
    
    activity_base = 120 
    if activity_min < 60: 
        activity_factor = (1 - (activity_min / 60)) * 0.05 * 0.6
    else:
        activity_factor = (activity_min - activity_base) / 60 * -0.1 * 0.6
        
    depletion_impact = steps_factor + activity_factor
    
    estimated_depletion_factor = max(0.6, min(1.0, 1.0 + depletion_impact))
    
    if steps == 0 and activity_min == 0:
        return s_fatigue.factor
    else:
        return estimated_depletion_factor

def calculate_filling_factor_from_diet(weight_kg, cho_day_minus_1_g, cho_day_minus_2_g, s_fatigue, s_sleep, steps_m1, min_act_m1, steps_m2, min_act_m2):
    CHO_BASE_GK = 5.0
    CHO_MAX_GK = 10.0
    CHO_MIN_GK = 2.5
    
    cho_day_minus_1_g = max(cho_day_minus_1_g, 1.0) 
    cho_day_minus_2_g = max(cho_day_minus_2_g, 1.0) 
    
    cho_day_minus_1_gk = cho_day_minus_1_g / weight_kg
    cho_day_minus_2_gk = cho_day_minus_2_g / weight_kg
    
    depletion_m1_factor = calculate_depletion_factor(steps_m1, min_act_m1, s_fatigue)
    depletion_m2_factor = calculate_depletion_factor(steps_m2, min_act_m2, s_fatigue)
    
    recovery_factor = (depletion_m1_factor * 0.7) + (depletion_m2_factor * 0.3)
    
    avg_cho_gk = (cho_day_minus_1_gk * 0.7) + (cho_day_minus_2_gk * 0.3)
    
    if avg_cho_gk >= CHO_MAX_GK:
        diet_factor_base = 1.25
    elif avg_cho_gk >= CHO_BASE_GK:
        diet_factor_base = 1.0 + (avg_cho_gk - CHO_BASE_GK) * (0.25 / (CHO_MAX_GK - CHO_BASE_GK))
    elif avg_cho_gk > CHO_MIN_GK:
        diet_factor_base = 0.5 + (avg_cho_gk - CHO_MIN_GK) * (0.5 / (CHO_BASE_GK - CHO_MIN_GK))
        diet_factor_base = max(0.5, diet_factor_base)
    else: 
        diet_factor_base = 0.5
    
    diet_factor_base = min(1.25, max(0.5, diet_factor_base)) 
    
    final_diet_depletion_factor = diet_factor_base * recovery_factor 
    combined_filling = final_diet_depletion_factor * s_sleep.factor
    
    return combined_filling, final_diet_depletion_factor, avg_cho_gk, cho_day_minus_1_gk, cho_day_minus_2_gk


def calculate_tank(subject: Subject):
    if subject.muscle_mass_kg is not None and subject.muscle_mass_kg > 0:
        total_muscle = subject.muscle_mass_kg
        muscle_source_note = "Massa Muscolare Totale (SMM) fornita dall'utente."
    else:
        lbm = subject.lean_body_mass
        total_muscle = lbm * subject.muscle_fraction
        muscle_source_note = "Massa Muscolare Totale stimata da Peso/BF/Sesso."

    active_muscle = total_muscle * subject.sport.val
    
    creatine_multiplier = 1.10 if subject.uses_creatine else 1.0
    base_muscle_glycogen = active_muscle * subject.glycogen_conc_g_kg
    max_total_capacity = (base_muscle_glycogen * 1.25 * creatine_multiplier) + 100.0
    
    final_filling_factor = subject.filling_factor * subject.menstrual_phase.factor
    current_muscle_glycogen = base_muscle_glycogen * creatine_multiplier * final_filling_factor
    
    max_physiological_limit = active_muscle * 35.0
    if current_muscle_glycogen > max_physiological_limit:
        current_muscle_glycogen = max_physiological_limit
    
    liver_fill_factor = 1.0
    liver_correction_note = None
    
    if subject.filling_factor <= 0.6: 
        liver_fill_factor = 0.6
        
    if subject.glucose_mg_dl is not None:
        if subject.glucose_mg_dl < 70:
            liver_fill_factor = 0.2
            liver_correction_note = "Criticità Epatica (Glicemia < 70 mg/dL)"
        elif subject.glucose_mg_dl < 85:
            liver_fill_factor = min(liver_fill_factor, 0.5)
            liver_correction_note = "Riduzione Epatica (Glicemia 70-85 mg/dL)"
    
    current_liver_glycogen = subject.liver_glycogen_g * liver_fill_factor
    total_actual_glycogen = current_muscle_glycogen + current_liver_glycogen

    return {
        "active_muscle_kg": active_muscle,
        "max_capacity_g": max_total_capacity,         
        "actual_available_g": total_actual_glycogen,   
        "muscle_glycogen_g": current_muscle_glycogen,
        "liver_glycogen_g": current_liver_glycogen,
        "concentration_used": subject.glycogen_conc_g_kg,
        "fill_pct": (total_actual_glycogen / max_total_capacity) * 100 if max_total_capacity > 0 else 0,
        "creatine_bonus": subject.uses_creatine,
        "liver_note": liver_correction_note,
        "muscle_source_note": muscle_source_note
    }

def estimate_max_exogenous_oxidation(height_cm, weight_kg, ftp_watts, mix_type: ChoMixType):
    base_rate = 0.8 
    
    if height_cm > 170:
        base_rate += (height_cm - 170) * 0.015
    if ftp_watts > 200:
        base_rate += (ftp_watts - 200) * 0.0015
    
    ox_factor = mix_type.ox_factor
    max_rate_gh = mix_type.max_rate_gh
    
    estimated_rate_gh = base_rate * 60 * ox_factor
    
    final_rate_g_min = min(estimated_rate_gh / 60, max_rate_gh / 60)
    
    return final_rate_g_min

def calculate_rer_polynomial(intensity_factor):
    if_val = intensity_factor
    rer = (
        -0.000000149 * (if_val**6) + 
        141.538462237 * (if_val**5) - 
        565.128206259 * (if_val**4) + 
        890.333333976 * (if_val**3) - 
        691.67948706 * (if_val**2) + 
        265.460857558 * if_val - 
        39.525121144
    )
    return max(0.70, min(1.15, rer))

def simulate_metabolism(
    subject_data, 
    duration_min, 
    constant_carb_intake_g_h, 
    cho_per_unit_g, 
    crossover_pct, 
    tau_absorption, 
    subject_obj, 
    activity_params,
    oxidation_efficiency_input=0.80, 
    custom_max_exo_rate=None,
    mix_type_input=ChoMixType.GLUCOSE_ONLY,
    intensity_series=None
):
    tank_g = subject_data['actual_available_g']
    results = []
    
    initial_muscle_glycogen = subject_data['muscle_glycogen_g']
    initial_liver_glycogen = subject_data['liver_glycogen_g']
    
    current_muscle_glycogen = initial_muscle_glycogen
    current_liver_glycogen = initial_liver_glycogen
    
    mode = activity_params.get('mode', 'cycling')
    gross_efficiency = activity_params.get('efficiency', 22.0)
    
    avg_power = activity_params.get('avg_watts', 200)
    ftp_watts = activity_params.get('ftp_watts', 250) 
    avg_hr = activity_params.get('avg_hr', 150)
    max_hr = activity_params.get('max_hr', 185)
    
    intensity_factor_reference = activity_params.get('intensity_factor', 0.8)
    
    if mode == 'cycling':
        kcal_per_min_base = (avg_power * 60) / 4184 / (gross_efficiency / 100.0)
    elif mode == 'running':
        speed_kmh = activity_params.get('speed_kmh', 10.0)
        weight = subject_obj.weight_kg
        kcal_per_hour = 1.0 * weight * speed_kmh
        kcal_per_min_base = kcal_per_hour / 60.0
    else:
        vo2_operating = subject_obj.vo2max_absolute_l_min * intensity_factor_reference
        kcal_per_min_base = vo2_operating * 5.0
        
    is_lab_data = activity_params.get('use_lab_data', False)
    lab_cho_rate = activity_params.get('lab_cho_g_h', 0) / 60.0
    lab_fat_rate = activity_params.get('lab_fat_g_h', 0) / 60.0
    
    crossover_pct = activity_params.get('crossover_pct', 70)
    crossover_if = crossover_pct / 100.0
    
    if custom_max_exo_rate is not None:
        max_exo_rate_g_min = custom_max_exo_rate 
    else:
        max_exo_rate_g_min = estimate_max_exogenous_oxidation(
            subject_obj.height_cm, 
            subject_obj.weight_kg, 
            ftp_watts,
            mix_type_input
        )
    
    oxidation_efficiency = oxidation_efficiency_input
    
    total_fat_burned_g = 0.0
    gut_accumulation_total = 0.0
    current_exo_oxidation_g_min = 0.0 
    
    alpha = 1 - np.exp(-1.0 / tau_absorption)
    
    total_muscle_used = 0.0
    total_liver_used = 0.0
    total_exo_used = 0.0
    
    total_intake_cumulative = 0.0
    total_exo_oxidation_cumulative = 0.0
    
    units_per_hour = constant_carb_intake_g_h / cho_per_unit_g if cho_per_unit_g > 0 else 0
    intake_interval_min = round(60 / units_per_hour) if units_per_hour > 0 else duration_min + 1
    
    is_input_zero = constant_carb_intake_g_h == 0
    
    for t in range(int(duration_min) + 1):
        
        current_intensity_factor = intensity_factor_reference
        if intensity_series is not None and t < len(intensity_series):
            current_intensity_factor = intensity_series[t]
        
        current_kcal_demand = 0.0
        
        if mode == 'cycling':
            instant_power = current_intensity_factor * ftp_watts
            current_eff = gross_efficiency
            if t > 60: 
                loss = (t - 60) * 0.02
                current_eff = max(15.0, gross_efficiency - loss)
            current_kcal_demand = (instant_power * 60) / 4184 / (current_eff / 100.0)
            
        else: 
            demand_scaling = current_intensity_factor / intensity_factor_reference if intensity_factor_reference > 0 else 1.0
            
            drift_factor = 1.0
            if t > 60:
                drift_factor += (t - 60) * 0.0005 
            
            current_kcal_demand = kcal_per_min_base * drift_factor * demand_scaling
        
        instantaneous_input_g_min = 0.0 
        
        if not is_input_zero and intake_interval_min <= duration_min and t > 0 and t % intake_interval_min == 0:
            instantaneous_input_g_min = cho_per_unit_g 
        
        target_exo_oxidation_limit_g_min = max_exo_rate_g_min * oxidation_efficiency
        
        if t > 0:
            if is_input_zero:
                current_exo_oxidation_g_min *= (1 - alpha) 
            else:
                current_exo_oxidation_g_min += alpha * (target_exo_oxidation_limit_g_min - current_exo_oxidation_g_min)
            
            if current_exo_oxidation_g_min < 0:
                current_exo_oxidation_g_min = 0.0
        else:
            current_exo_oxidation_g_min = 0.0
            
        if t > 0:
            gut_accumulation_total += (instantaneous_input_g_min * oxidation_efficiency) - current_exo_oxidation_g_min
            if gut_accumulation_total < 0: gut_accumulation_total = 0 

            total_intake_cumulative += instantaneous_input_g_min 
            total_exo_oxidation_cumulative += current_exo_oxidation_g_min
        
        if is_lab_data:
            fatigue_mult = 1.0 + ((t - 30) * 0.0005) if t > 30 else 1.0 
            total_cho_demand = lab_cho_rate * fatigue_mult 

            kcal_cho_demand = total_cho_demand * 4.1
            
            glycogen_burned_per_min = total_cho_demand - current_exo_oxidation_g_min
            min_endo = total_cho_demand * 0.2 
            if glycogen_burned_per_min < min_endo: glycogen_burned_per_min = min_endo
            lab_fat_rate_min = lab_fat_rate / 60
            cho_ratio = total_cho_demand / (total_cho_demand + lab_fat_rate_min) if (total_cho_demand + lab_fat_rate_min) > 0 else 0
            rer = 0.7 + (0.3 * cho_ratio) 
        
        else:
            effective_if_for_rer = current_intensity_factor + ((75.0 - crossover_pct) / 100.0)
            if effective_if_for_rer < 0.3: effective_if_for_rer = 0.3
            
            rer = calculate_rer_polynomial(effective_if_for_rer)
            base_cho_ratio = (rer - 0.70) * 3.45
            base_cho_ratio = max(0.0, min(1.0, base_cho_ratio))
            
            current_cho_ratio = base_cho_ratio
            if current_intensity_factor < 0.85 and t > 60:
                hours_past = (t - 60) / 60.0
                metabolic_shift = 0.05 * (hours_past ** 1.2) 
                current_cho_ratio = max(0.05, base_cho_ratio - metabolic_shift)
            
            cho_ratio = current_cho_ratio
            fat_ratio = 1.0 - cho_ratio
            
            kcal_cho_demand = current_kcal_demand * cho_ratio
        
        total_cho_g_min = kcal_cho_demand / 4.1
        kcal_from_exo = current_exo_oxidation_g_min * 3.75 
        
        muscle_fill_state = current_muscle_glycogen / initial_muscle_glycogen if initial_muscle_glycogen > 0 else 0
        muscle_contribution_factor = math.pow(muscle_fill_state, 0.6) 
        
        muscle_usage_g_min = total_cho_g_min * muscle_contribution_factor
        if current_muscle_glycogen <= 0: muscle_usage_g_min = 0
        
        blood_glucose_demand_g_min = total_cho_g_min - muscle_usage_g_min
        
        from_exogenous = min(blood_glucose_demand_g_min, current_exo_oxidation_g_min)
        
        remaining_blood_demand = blood_glucose_demand_g_min - from_exogenous
        max_liver_output = 1.2 
        from_liver = min(remaining_blood_demand, max_liver_output)
        if current_liver_glycogen <= 0: from_liver = 0
        
        if t > 0:
            current_muscle_glycogen -= muscle_usage_g_min
            current_liver_glycogen -= from_liver
            
            if current_muscle_glycogen < 0: current_muscle_glycogen = 0
            if current_liver_glycogen < 0: current_liver_glycogen = 0
            
            if not is_lab_data:
                fat_ratio_used = 1.0 - cho_ratio
                total_fat_burned_g += (current_kcal_demand * fat_ratio_used) / 9.0
            else:
                total_fat_burned_g += lab_fat_rate
            
            total_muscle_used += muscle_usage_g_min
            total_liver_used += from_liver
            total_exo_used += from_exogenous
            
        status_label = "Ottimale"
        if current_liver_glycogen < 20: status_label = "CRITICO (Ipoglicemia)"
        elif current_muscle_glycogen < 100: status_label = "Warning (Gambe Vuote)"
            
        exo_oxidation_g_h = from_exogenous * 60
        
        g_muscle = muscle_usage_g_min
        g_liver = from_liver
        g_exo = from_exogenous
        fat_ratio_used_local = 1.0 - cho_ratio if not is_lab_data else (lab_fat_rate / 60 * 9.0) / current_kcal_demand if current_kcal_demand > 0 else 0.0
        g_fat = (current_kcal_demand * fat_ratio_used_local / 9.0)
        
        total_g_min = g_muscle + g_liver + g_exo + g_fat
        if total_g_min == 0: total_g_min = 1.0 
        
        results.append({
            "Time (min)": t,
            "Glicogeno Muscolare (g)": muscle_usage_g_min * 60, 
            "Glicogeno Epatico (g)": from_liver * 60,
            "Carboidrati Esogeni (g)": exo_oxidation_g_h, 
            "Ossidazione Lipidica (g)": lab_fat_rate * 60 if is_lab_data else ((current_kcal_demand * (1.0 - cho_ratio)) / 9.0) * 60,
            
            "Pct_Muscle": f"{(g_muscle / total_g_min * 100):.1f}%",
            "Pct_Liver": f"{(g_liver / total_g_min * 100):.1f}%",
            "Pct_Exo": f"{(g_exo / total_g_min * 100):.1f}%",
            "Pct_Fat": f"{(g_fat / total_g_min * 100):.1f}%",

            "Residuo Muscolare": current_muscle_glycogen,
            "Residuo Epatico": current_liver_glycogen,
            "Residuo Totale": current_muscle_glycogen + current_liver_glycogen, 
            "Target Intake (g/h)": constant_carb_intake_g_h, 
            "Gut Load": gut_accumulation_total,
            "Stato": status_label,
            "CHO %": cho_ratio * 100,
            "Intake Cumulativo (g)": total_intake_cumulative,
            "Ossidazione Cumulativa (g)": total_exo_oxidation_cumulative,
            "Intensity Factor (IF)": current_intensity_factor 
        })
        
    total_kcal_final = current_kcal_demand * 60 
    
    final_total_glycogen = current_muscle_glycogen + current_liver_glycogen

    stats = {
        "final_muscle": current_muscle_glycogen,
        "final_liver": current_liver_glycogen,
        "final_glycogen": final_total_glycogen, 
        "total_muscle_used": total_muscle_used,
        "total_liver_used": total_liver_used,
        "total_exo_used": total_exo_used,
        "fat_total_g": total_fat_burned_g,
        "kcal_total_h": total_kcal_final,
        "gut_accumulation": (gut_accumulation_total / duration_min) * 60 if duration_min > 0 else 0,
        "max_exo_capacity": max_exo_rate_g_min * 60,
        "intensity_factor": intensity_factor_reference,
        "avg_rer": rer,
        "gross_efficiency": gross_efficiency,
        "intake_g_h": constant_carb_intake_g_h,
        "cho_pct": cho_ratio * 100
    }

    return pd.DataFrame(results), stats

# --- LOGICA DI PARSING ZWO ---

def parse_zwo_file(uploaded_file, ftp_watts, thr_hr, sport_type):
    
    try:
        xml_content = uploaded_file.getvalue().decode('utf-8')
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        st.error("Errore di parsing: il file ZWO non è un XML valido.")
        return [], 0, 0, 0
    except Exception as e:
        st.error(f"Errore nella lettura del file: {e}")
        return [], 0, 0, 0

    zwo_sport_tag = root.findtext('sportType')
    
    if zwo_sport_tag:
        if zwo_sport_tag.lower() == 'bike' and sport_type != SportType.CYCLING:
            st.warning(f"⚠️ ATTENZIONE: Hai selezionato {sport_type.label} nel Tab 1, ma il file ZWO è per BICI. I calcoli useranno la soglia di {sport_type.label}, ma potrebbero essere imprecisi.")
        elif zwo_sport_tag.lower() == 'run' and sport_type != SportType.RUNNING:
            st.warning(f"⚠️ ATTENZIONE: Hai selezionato {sport_type.label} nel Tab 1, ma il file ZWO è per CORSA. I calcoli useranno la soglia di {sport_type.label}, ma potrebbero essere imprecisi.")

    
    intensity_series = [] 
    total_duration_sec = 0
    total_weighted_if = 0
    
    for steady_state in root.findall('.//SteadyState'):
        try:
            duration_sec = int(steady_state.get('Duration'))
            power_ratio = float(steady_state.get('Power'))
            
            duration_min_segment = math.ceil(duration_sec / 60)
            
            intensity_factor = power_ratio 
            
            for _ in range(duration_min_segment):
                intensity_series.append(intensity_factor)
            
            total_duration_sec += duration_sec
            total_weighted_if += intensity_factor * (duration_sec / 60) 

        except Exception as e:
            st.error(f"Errore durante l'analisi di un segmento SteadyState: {e}")
            continue

    total_duration_min = math.ceil(total_duration_sec / 60)
    
    if total_duration_min > 0:
        avg_if = total_weighted_if / total_duration_min
        
        if sport_type == SportType.CYCLING:
            avg_power = avg_if * ftp_watts
            avg_hr = 0
        elif sport_type == SportType.RUNNING:
            avg_hr = avg_if * thr_hr
            avg_power = 0
        else: 
            avg_hr = avg_if * st.session_state.get('max_hr_input', 185) * 0.85 
            avg_power = 0
            
        return intensity_series, total_duration_min, avg_power, avg_hr
    
    return [], 0, 0, 0

# --- FUNZIONI PER LE ZONE DI ALLENAMENTO ---

def calculate_zones_cycling(ftp):
    return [
        {"Zona": "Z1 - Recupero Attivo", "Range %": "< 55%", "Valore": f"< {int(ftp*0.55)} W"},
        {"Zona": "Z2 - Endurance (Fondo Lento)", "Range %": "56 - 75%", "Valore": f"{int(ftp*0.56)} - {int(ftp*0.75)} W"},
        {"Zona": "Z3 - Tempo (Medio)", "Range %": "76 - 90%", "Valore": f"{int(ftp*0.76)} - {int(ftp*0.90)} W"},
        {"Zona": "Z4 - Soglia (FTP)", "Range %": "91 - 105%", "Valore": f"{int(ftp*0.91)} - {int(ftp*1.05)} W"},
        {"Zona": "Z5 - VO2max", "Range %": "106 - 120%", "Valore": f"{int(ftp*1.06)} - {int(ftp*1.20)} W"},
        {"Zona": "Z6 - Capacità Anaerobica", "Range %": "121 - 150%", "Valore": f"{int(ftp*1.21)} - {int(ftp*1.50)} W"},
        {"Zona": "Z7 - Potenza Neuromuscolare", "Range %": "> 150%", "Valore": f"> {int(ftp*1.50)} W"}
    ]

def calculate_zones_running_hr(thr):
    return [
        {"Zona": "Z1 - Recupero", "Range %": "< 85% LTHR", "Valore": f"< {int(thr*0.85)} bpm"},
        {"Zona": "Z2 - Aerobico (Fondo Lento)", "Range %": "85 - 89% LTHR", "Valore": f"{int(thr*0.85)} - {int(thr*0.89)} bpm"},
        {"Zona": "Z3 - Tempo (Medio)", "Range %": "90 - 94% LTHR", "Valore": f"{int(thr*0.90)} - {int(thr*0.94)} bpm"},
        {"Zona": "Z4 - Sub-Soglia", "Range %": "95 - 99% LTHR", "Valore": f"{int(thr*0.95)} - {int(thr*0.99)} bpm"},
        {"Zona": "Z5a - Super-Soglia (FTP)", "Range %": "100 - 102% LTHR", "Valore": f"{int(thr*1.00)} - {int(thr*1.02)} bpm"},
        {"Zona": "Z5b - Capacità Aerobica", "Range %": "103 - 106% LTHR", "Valore": f"{int(thr*1.03)} - {int(thr*1.06)} bpm"},
        {"Zona": "Z5c - Potenza Anaerobica", "Range %": "> 106% LTHR", "Valore": f"> {int(thr*1.06)} bpm"}
    ]

# --- FUNZIONE DI CALCOLO SETTIMANALE ---
def calculate_weekly_balance(initial_muscle, initial_liver, max_muscle, max_liver, weekly_schedule, subject_weight, vo2max):
    
    LIVER_DRAIN_RATE = 4.5 
    DAILY_NEAT_CHO = 1.2 * subject_weight
    
    SYNTHESIS_EFFICIENCY = 0.95
    
    daily_status = []
    
    current_muscle = initial_muscle
    current_liver = initial_liver
    
    days = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    
    for i, day in enumerate(days):
        day_data = weekly_schedule[i]
        
        activity_type = day_data['activity']
        duration = day_data['duration']
        intensity = day_data['intensity'] 
        cho_in = day_data['cho_in']
        
        total_basal_drain = (24 * LIVER_DRAIN_RATE) + DAILY_NEAT_CHO
        
        exercise_drain_muscle = 0
        exercise_drain_liver = 0
        
        if activity_type != "Riposo" and duration > 0:
            if intensity == "Bassa (Z1-Z2)":
                rel_intensity = 0.5
                cho_pct = 0.25 
            elif intensity == "Media (Z3)":
                rel_intensity = 0.7
                cho_pct = 0.65 
            else: 
                rel_intensity = 0.85
                cho_pct = 0.95 
                
            kcal_min = (vo2max * rel_intensity * subject_weight / 1000) * 5.0
            total_kcal = kcal_min * duration
            total_cho_burned = (total_kcal * cho_pct) / 4.0 
            
            liver_fraction = 0.15 
            
            exercise_drain_liver = total_cho_burned * liver_fraction
            exercise_drain_muscle = total_cho_burned * (1 - liver_fraction)
            
        total_daily_consumption = total_basal_drain + exercise_drain_liver + exercise_drain_muscle
        net_balance = cho_in * SYNTHESIS_EFFICIENCY - total_daily_consumption
        
        effective_input = cho_in * SYNTHESIS_EFFICIENCY
        
        drain_liver_total = total_basal_drain + exercise_drain_liver
        drain_muscle_total = exercise_drain_muscle
        
        if effective_input >= drain_liver_total:
            surplus_after_liver_needs = effective_input - drain_liver_total
            current_liver = current_liver 
            
            liver_space = max_liver - current_liver
            if surplus_after_liver_needs >= liver_space:
                current_liver = max_liver
                surplus_for_muscle = surplus_after_liver_needs - liver_space
            else:
                current_liver += surplus_after_liver_needs
                surplus_for_muscle = 0
                
        else:
            deficit = drain_liver_total - effective_input
            current_liver -= deficit
            surplus_for_muscle = 0 
            
        current_muscle -= drain_muscle_total 
        current_muscle += surplus_for_muscle 
        
        if current_muscle > max_muscle: current_muscle = max_muscle
        if current_liver > max_liver: current_liver = max_liver
        
        if current_muscle < 0: current_muscle = 0
        if current_liver < 0: current_liver = 0
            
        daily_status.append({
            "Giorno": day,
            "Glicogeno Muscolare": round(current_muscle),
            "Glicogeno Epatico": round(current_liver),
            "Totale": round(current_muscle + current_liver),
            "Allenamento": f"{activity_type} ({duration} min)" if activity_type != "Riposo" else "Riposo",
            "CHO In": cho_in,
            "Consumo Stimato": round(total_daily_consumption),
            "Bilancio Netto": round(net_balance)
        })
        
    return pd.DataFrame(daily_status)


# --- 3. INTERFACCIA UTENTE ---

st.set_page_config(page_title="Glycogen Simulator Pro", layout="wide")

st.title("Glycogen Simulator Pro")
st.markdown("Strumento di stima delle riserve energetiche e simulazione del metabolismo sotto sforzo.")

# --- NOTE TECNICHE REINTRODOTTE ---
with st.expander("📘 Note Tecniche & Fonti Scientifiche"):
    st.info("""
    **1. Stima Riserve & Capacità di Stoccaggio**
    * **Stima della Concentrazione (g/kg):** Si basa sulla correlazione tra il fitness aerobico (VO2max) e la densità di stoccaggio muscolare, riflettendo la capacità di adattamento cellulare (Burke et al., 2017).
    * **Capacità Massima (Fattore 1.25):** La supercompensazione del glicogeno si ottiene con carichi di CHO $>8 \text{ g/kg/die}$ in $36-48$ ore, portando le riserve totali oltre i livelli basali (Bergström et al., 1967; Burke et al., 2017).
    * **Creatina:** La supplementazione di creatina (tipicamente con protocolli di carico acuto di $20 \text{ g/die}$ per $5-6$ giorni o mantenimento di $3-5 \text{ g/die}$) è associata a un aumento aggiuntivo ($\sim 10\%$) nella capacità totale di stoccaggio del glicogeno, a condizione che la saturazione muscolare sia stata raggiunta (Roberts et al., 2016; Burke et al., 2017).
    
    ---
    
    **2. Sviluppi Recenti & Personalizzazione**
    
    * **Peso dei Fattori (RER):** Studi di modellazione (Rothschild et al., 2022) indicano che **Sesso** e **Durata** sono i maggiori determinanti del mix energetico in gara. La **Dieta CHO/Fat giornaliera** ha un'influenza sul mix energetico *maggiore* rispetto all'assunzione CHO *durante* l'esercizio.
    * **Variabilità e Rischio GI:** L'alta variabilità nell'ossidazione dei CHO esogeni ($\text{0.5-1.5 g/min}$) e l'accumulo conseguente sono i predittori principali del distress GI (Podlogar et al., 2025).
    * **Mix di Carboidrati:** L'uso di miscele Glucosio:Fruttosio (es. 2:1 o 1:0.8) sfrutta trasportatori multipli (SGLT1 e GLUT5), aumentando il limite di ossidazione fino a $1.5-1.7 \text{ g/min}$ (Jeukendrup, 2004).
    """)
# --- FINE NOTE TECNICHE REINTRODOTTE ---

tab1, tab2, tab3 = st.tabs(["1. Profilo Base & Capacità", "2. Preparazione & Diario", "3. Simulazione & Strategia"])

# --- TAB 1: PROFILO BASE & CAPACITÀ ---
with tab1:
    col_in, col_res = st.columns([1, 2])
    
    with col_in:
        # =========================================================================
        # SEZIONE 1: DATI ANTROPOMETRICI E BASE
        # =========================================================================
        st.subheader("1. Dati Antropometrici")
        weight = st.slider("Peso Corporeo (kg)", 45.0, 100.0, 74.0, 0.5) # DEFAULT: 74.0 kg
        height = st.slider("Altezza (cm)", 150, 210, 187, 1) # DEFAULT: 187 cm
        bf = st.slider("Massa Grassa (%)", 4.0, 30.0, 11.0, 0.5) / 100.0 # DEFAULT: 11.0%
        
        sex_map = {s.value: s for s in Sex}
        s_sex = sex_map[st.radio("Sesso", list(sex_map.keys()), horizontal=True)]
        
        # --- NUOVO INPUT PER MASSA MUSCOLARE REALE ---
        use_smm = st.checkbox("Usa Massa Muscolare (SMM) da esame strumentale (Impedenziometria/DEXA)",
                              help="Seleziona questa opzione per sostituire la stima interna (basata su Peso/BF/Sesso) con un valore misurato direttamente.")
        muscle_mass_input = None
        if use_smm:
            muscle_mass_input = st.number_input(
                "Massa Muscolare Totale (SMM) [kg]",
                min_value=10.0, max_value=60.0, value=37.4, step=0.1, # DEFAULT: 37.4 kg
                help="Inserire la massa muscolare scheletrica totale misurata (es. da DEXA o BIA)."
            )
        # --- FINE NUOVO INPUT ---
        
        st.markdown("---")
        
        # =========================================================================
        # SEZIONE 2: CAPACITÀ MASSIMA DI STOCCAGGIO (Tank Max)
        # =========================================================================
        st.subheader("2. Capacità di Stoccaggio Massima (Tank)")
        
        # 2a. Metodo di calcolo della concentrazione
        st.write("**Stima Concentrazione Glicogeno Muscolare**")
        estimation_method = st.radio("Metodo di calcolo:", ["Basato su Livello", "Basato su VO2max"], label_visibility="collapsed")
        
        # Inizializzazione variabili per sicurezza scope
        vo2_input = 60.0 # DEFAULT: 60
        calculated_conc = get_concentration_from_vo2max(vo2_input) # DEFAULT: basato su 60

        # Mappa dei livelli per trovare l'indice corretto per il default
        status_map = {s.label: s for s in TrainingStatus}
        default_status_label = "Avanzato / Competitivo" # Corrisponde a ~22.0 g/kg (~60 VO2max)
        default_status_index = list(status_map.keys()).index(default_status_label)

        
        if estimation_method == "Basato su Livello":
            s_status = status_map[st.selectbox("Livello Atletico", list(status_map.keys()), index=default_status_index, key='lvl_status')]
            calculated_conc = s_status.val
            # Calcoliamo il vo2_input come proxy per la visualizzazione (non usato per il calcolo ma utile per la UI)
            vo2_input = 30 + ((calculated_conc - 13.0) / 0.24)
        else:
            # Se basato su VO2max, prendiamo il valore dallo slider
            vo2_input = st.slider("VO2max (ml/kg/min)", 30, 85, 60, step=1) # DEFAULT: 60
            calculated_conc = get_concentration_from_vo2max(vo2_input)
            
        # Mostra il risultato della stima
        lvl_desc = ""
        if calculated_conc < 15: lvl_desc = "Sedentario"
        elif calculated_conc < 18: lvl_desc = "Amatore"
        elif calculated_conc < 22: lvl_desc = "Allenato"
        elif calculated_conc < 24: lvl_desc = "Avanzato"
        else: lvl_desc = "Elite"
        st.caption(f"Concentrazione stimata: **{calculated_conc:.1f} g/kg** ({lvl_desc})")

        # 2b. Disciplina Sportiva
        sport_map = {s.label: s for s in SportType}
        default_sport_label = "Ciclismo (Prevalenza arti inferiori)"
        default_sport_index = list(sport_map.keys()).index(default_sport_label)
        s_sport = sport_map[st.selectbox("Disciplina Sportiva", list(sport_map.keys()), index=default_sport_index)]
        
        # =========================================================================
        # SEZIONE 2c: DATI DI SOGLIA SPECIFICI PER DISCIPLINA (Nuova posizione)
        # =========================================================================
        st.markdown("#### Dati di Soglia per la Simulazione")
        
        # Inizializzazione delle variabili di soglia per l'uso nel Tab 3
        ftp_watts_input = 265 # DEFAULT
        thr_hr_input = 170 # DEFAULT
        max_hr_input = 185 # DEFAULT
        
        zones_data = [] # Dati per la tabella zone
        
        zone_def_method = st.radio("Definizione Zone:", ["Standard (Calcolate)", "Personalizzate (Manuale)"], horizontal=True)

        with st.expander("Inserisci le Tue Soglie e Visualizza Zone", expanded=True):
            if s_sport == SportType.CYCLING:
                # MODIFICA: FTP è l'input primario per IF
                ftp_watts_input = st.number_input("Functional Threshold Power (FTP) [Watt]", 100, 600, 265, step=5)
                st.caption(f"La FTP è usata come soglia per l'Intensity Factor (IF).")
                
                if zone_def_method == "Standard (Calcolate)":
                    zones_data = calculate_zones_cycling(ftp_watts_input)
                else:
                    # Input manuale per zone personalizzate (semplificato a 5 zone per brevità)
                    st.caption("Inserisci il limite SUPERIORE di ogni zona in Watt.")
                    z1_lim = st.number_input("Limite Z1 (Recupero)", 0, 1000, int(ftp_watts_input*0.55))
                    z2_lim = st.number_input("Limite Z2 (Endurance)", 0, 1000, int(ftp_watts_input*0.75))
                    z3_lim = st.number_input("Limite Z3 (Tempo)", 0, 1000, int(ftp_watts_input*0.90))
                    z4_lim = st.number_input("Limite Z4 (Soglia)", 0, 1000, int(ftp_watts_input*1.05))
                    
                    zones_data = [
                        {"Zona": "Z1 - Recupero", "Range %": "Custom", "Valore": f"< {z1_lim} W"},
                        {"Zona": "Z2 - Endurance", "Range %": "Custom", "Valore": f"{z1_lim+1} - {z2_lim} W"},
                        {"Zona": "Z3 - Tempo", "Range %": "Custom", "Valore": f"{z2_lim+1} - {z3_lim} W"},
                        {"Zona": "Z4 - Soglia", "Range %": "Custom", "Valore": f"{z3_lim+1} - {z4_lim} W"},
                        {"Zona": "Z5+ - Sovrasoglia", "Range %": "Custom", "Valore": f"> {z4_lim} W"}
                    ]
            
            elif s_sport == SportType.RUNNING:
                c_thr, c_max = st.columns(2)
                # MODIFICA: Uso THR come dato primario per IF nella corsa
                thr_hr_input = c_thr.number_input("Soglia Anaerobica (THR/LT2) [BPM]", 100, 220, 170, 1)
                max_hr_input = c_max.number_input("Frequenza Cardiaca Max (BPM)", 100, 220, 185, 1)
                st.caption(f"La Soglia Anaerobica è usata per calcolare l'IF (FC media / THR).")
                
                if zone_def_method == "Standard (Calcolate)":
                    zones_data = calculate_zones_running_hr(thr_hr_input)
                else:
                    st.caption("Inserisci il limite SUPERIORE di ogni zona in BPM.")
                    z1_lim = st.number_input("Limite Z1 (Recupero)", 0, 250, int(thr_hr_input*0.85))
                    z2_lim = st.number_input("Limite Z2 (Aerobico)", 0, 250, int(thr_hr_input*0.89))
                    z3_lim = st.number_input("Limite Z3 (Tempo)", 0, 250, int(thr_hr_input*0.94))
                    z4_lim = st.number_input("Limite Z4 (Soglia)", 0, 250, int(thr_hr_input*0.99))
                    
                    zones_data = [
                        {"Zona": "Z1 - Recupero", "Range %": "Custom", "Valore": f"< {z1_lim} bpm"},
                        {"Zona": "Z2 - Aerobico", "Range %": "Custom", "Valore": f"{z1_lim+1} - {z2_lim} bpm"},
                        {"Zona": "Z3 - Tempo", "Range %": "Custom", "Valore": f"{z2_lim+1} - {z3_lim} bpm"},
                        {"Zona": "Z4 - Soglia", "Range %": "Custom", "Valore": f"{z3_lim+1} - {z4_lim} bpm"},
                        {"Zona": "Z5+ - Sovrasoglia", "Range %": "Custom", "Valore": f"> {z4_lim} bpm"}
                    ]

            else: # TRIATHLON, SWIMMING, XC_SKIING (usano HR Max/Avg per proxy)
                c_thr, c_max = st.columns(2)
                max_hr_input = st.number_input("Frequenza Cardiaca Max (BPM)", 100, 220, 185, 1, key='max_hr_input_general')
                thr_hr_input = st.number_input("Soglia Aerobica (LT1/VT1) [BPM]", 80, max_hr_input-5, 150, 1, key='thr_hr_input_general') 
                if zone_def_method == "Standard (Calcolate)":
                    zones_data = calculate_zones_running_hr(thr_hr_input) # Fallback
                else:
                    st.caption("Personalizzazione disponibile per Ciclismo e Corsa.")

            if zones_data:
                st.markdown("**Le tue Zone di Allenamento:**")
                st.table(pd.DataFrame(zones_data))
        
        # Salvataggio delle soglie nello stato di sessione per il Tab 3
        st.session_state['ftp_watts_input'] = ftp_watts_input
        st.session_state['thr_hr_input'] = thr_hr_input
        st.session_state['max_hr_input'] = max_hr_input


        # =========================================================================
        # SEZIONE 3: FATTORI AVANZATI DI CAPACITÀ
        # =========================================================================
        with st.expander("Fattori Avanzati di Capacità (Aumento potenziale Max)"):
            use_creatine = st.checkbox("Supplementazione Creatina", help="Aumento volume cellulare e capacità di stoccaggio stimata (+10%).")
            s_menstrual = MenstrualPhase.NONE
            if s_sex == Sex.FEMALE:
                menstrual_map = {m.label: m for m in MenstrualPhase}
                s_menstrual = menstrual_map[st.selectbox("Fase Ciclo Mestruale", list(menstrual_map.keys()), index=0)]
        
        st.markdown("---")
        
        # Variabili necessarie per il calcolo finale (inizializzate)
        combined_filling = 1.0 
        
        # Creazione Struttura Subject e Calcolo finale del Tank (PARZIALE)
        vo2_abs = (vo2_input * weight) / 1000
        
        subject = Subject(
            weight_kg=weight, 
            height_cm=height,
            body_fat_pct=bf, sex=s_sex, 
            glycogen_conc_g_kg=calculated_conc, sport=s_sport, 
            liver_glycogen_g=100.0, # Valore di riposo per calcolo max
            filling_factor=combined_filling, 
            uses_creatine=use_creatine,
            menstrual_phase=s_menstrual,
            glucose_mg_dl=None,
            vo2max_absolute_l_min=vo2_abs,
            muscle_mass_kg=muscle_mass_input 
        )
        
        tank_data = calculate_tank(subject)
        # Salviamo la struttura base e i dati del tank per il prossimo tab
        st.session_state['base_subject_struct'] = subject
        st.session_state['base_tank_data'] = tank_data 

    with col_res:
        st.subheader("Riepilogo Capacità Massima (Tank Max)")
        
        max_capacity_g = tank_data['max_capacity_g']
        
        st.write(f"**Capacità di Stoccaggio Teorica:** {int(max_capacity_g)} g")
        st.progress(100)
        
        c1, c2 = st.columns(2)
        c1.metric("Massa Muscolare Attiva", f"{tank_data['active_muscle_kg']:.1f} kg",
                  help="Massa muscolare che contribuisce all'attività fisica.")
        c2.metric("Energia Max Disponibile (CHO)", f"{int(max_capacity_g * 4.1)} kcal",
                  help="Calcolato assumendo pieno riempimento (fattore 1.25) e glicogeno epatico standard.")

        st.markdown("---")
        st.caption(f"Concentrazione muscolare base: **{calculated_conc:.1f} g/kg**")
        st.caption(f"Fonte Massa Muscolare: {tank_data['muscle_source_note']}")

# --- TAB 2: PREPARAZIONE & DIARIO (UNITO) ---
with tab2:
    if 'base_tank_data' not in st.session_state:
        st.warning("Completare prima il Tab '1. Profilo Base & Capacità'.")
        st.stop()
        
    weight = st.session_state['base_subject_struct'].weight_kg
    
    # Inizializza i dizionari per la selezione
    fatigue_map = {f.label: f for f in FatigueState}
    sleep_map = {s.label: s for s in SleepQuality}

    # Variabili CHO Inizializzate per prevenire NameError (assegnate alla media neutra)
    cho_g1 = weight * DietType.NORMAL.ref_value
    cho_g2 = weight * DietType.NORMAL.ref_value
    
    st.subheader("Stato Pre-Gara: Analisi Rapida (48h)")
    
    col_in_2, col_res_2 = st.columns([1, 1])
    
    with col_in_2:
        # SEZIONE 3: STATO NUTRIZIONALE
        st.markdown("**1. Stato Nutrizionale (Introito CHO 48h)**")
        st.info("La dieta degli ultimi 2 giorni ha l'influenza maggiore sul tuo metabolismo in gara (Rothschild et al., 2022).")
        
        diet_method = st.radio(
            "Metodo Calcolo:", 
            ["1. Veloce (Tipo Dieta)", "2. Preciso (Grammi CHO)"], 
            key='diet_calc_method', horizontal=True
        )
        
        if diet_method == "1. Veloce (Tipo Dieta)":
            diet_options_map = {d.label: d for d in DietType}
            selected_diet_label = st.selectbox("Introito Glucidico", list(diet_options_map.keys()), index=1, key='diet_type_select')
            
            s_diet = DietType.NORMAL
            for d in DietType:
                if selected_diet_label.startswith(d.label):
                    s_diet = d
                    break
            
            cho_g1 = weight * s_diet.ref_value
            cho_g2 = weight * s_diet.ref_value 
            
            temp_fatigue = FatigueState.RESTED
            temp_sleep = SleepQuality.GOOD
            
            _, diet_factor, avg_cho_gk, _, _ = calculate_filling_factor_from_diet(
                weight_kg=weight,
                cho_day_minus_1_g=cho_g1,
                cho_day_minus_2_g=cho_g2,
                s_fatigue=temp_fatigue, 
                s_sleep=temp_sleep,     
                steps_m1=0, min_act_m1=0, steps_m2=0, min_act_m2=0 
            )
            
        else:
            c_d2, c_d1 = st.columns(2)
            cho_day_minus_2_g = c_d2.number_input("CHO Giorno -2 (g)", 50, 800, 370, 10)
            cho_day_minus_1_g = c_d1.number_input("CHO Giorno -1 (g)", 50, 800, 370, 10)
            
            cho_g1 = cho_day_minus_1_g
            cho_g2 = cho_day_minus_2_g
            
            temp_fatigue = FatigueState.RESTED
            temp_sleep = SleepQuality.GOOD
            
            _, diet_factor, avg_cho_gk, _, _ = calculate_filling_factor_from_diet(
                weight_kg=weight,
                cho_day_minus_1_g=cho_day_minus_1_g,
                cho_day_minus_2_g=cho_day_minus_2_g,
                s_fatigue=temp_fatigue, 
                s_sleep=temp_sleep,     
                steps_m1=0, min_act_m1=0, steps_m2=0, min_act_m2=0 
            )

        # SEZIONE 4: RECUPERO
        st.markdown("**2. Condizione di Recupero**")
        default_sleep_label = "Sufficiente (6-7h)"
        default_sleep_index = list(sleep_map.keys()).index(default_sleep_label)
        
        s_fatigue = fatigue_map[st.selectbox("Carico di Lavoro (24h prec.)", list(fatigue_map.keys()), index=0, key='fatigue_final')]
        s_sleep = sleep_map[st.selectbox("Qualità del Sonno (24h prec.)", list(sleep_map.keys()), index=default_sleep_index, key='sleep_final')] 
        
        with st.expander("Dettagli Attività Motorio/Sportiva (Opzionale)"):
            c1, c2 = st.columns(2)
            steps_m1 = c1.number_input("Passi Giorno -1", 0, 20000, 5000, 500)
            min_act_m1 = c2.number_input("Minuti Sport Giorno -1", 0, 300, 30, 10)
            steps_m2 = 0
            min_act_m2 = 0

        combined_filling, diet_factor, avg_cho_gk, _, _ = calculate_filling_factor_from_diet(
            weight_kg=weight,
            cho_day_minus_1_g=cho_g1,
            cho_day_minus_2_g=cho_g2,
            s_fatigue=s_fatigue, 
            s_sleep=s_sleep,
            steps_m1=steps_m1, min_act_m1=min_act_m1, steps_m2=steps_m2, min_act_m2=min_act_m2
        )
        
        # SEZIONE 5: METABOLICO ACUTO
        st.markdown("**3. Stato Metabolico Acuto (Oggi)**")
        has_glucose = st.checkbox("Ho misurato la Glicemia", key='has_glucose_tab2')
        glucose_val = None
        is_fasted = False
        
        if has_glucose:
            glucose_val = st.number_input("Glicemia (mg/dL)", 40, 200, 90, 1)
        else:
            is_fasted = st.checkbox("Allenamento a Digiuno (Morning Fasted)", key='is_fasted_tab2')
        
        liver_val = 100.0
        if is_fasted: liver_val = 40.0 
        
        # RICALCOLO SUBJECT E TANK
        base_subject = st.session_state['base_subject_struct']
        subject = base_subject
        subject.liver_glycogen_g = liver_val
        subject.filling_factor = combined_filling
        subject.glucose_mg_dl = glucose_val
        
        tank_data = calculate_tank(subject)
        st.session_state['tank_data'] = tank_data 
        st.session_state['tank_g'] = tank_data['actual_available_g']
        st.session_state['subject_struct'] = subject 

    with col_res_2:
        st.markdown("### Riserve Disponibili")
        fill_pct = tank_data['fill_pct']
        st.metric("Livello Riempimento", f"{fill_pct:.1f}%")
        st.progress(int(fill_pct))
        
        if fill_pct < 60:
            st.error("Attenzione: Riserve ridotte. Rischio elevato.")
        elif fill_pct < 90:
            st.warning("Buono, ma non ottimale per gare lunghe.")
        else:
            st.success("Serbatoio Pieno (Ready to Race).")
            
        c1, c2 = st.columns(2)
        c1.metric("Muscolo (g)", int(tank_data['muscle_glycogen_g']))
        c2.metric("Fegato (g)", int(tank_data['liver_glycogen_g']))

    st.markdown("---")
    
    # --- DIARIO SETTIMANALE INTEGRATO ---
    st.subheader("Diario Settimanale del Glicogeno (Pianificazione)")
    st.info("Simula l'andamento del glicogeno su 7 giorni per pianificare la settimana di gara (Tapering).")
    
    with st.expander("Compila il Diario Settimanale", expanded=False):
        days = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
        weekly_schedule = []
        
        # Layout a griglia per i giorni
        for day in days:
            st.markdown(f"**{day}**")
            c1, c2, c3, c4 = st.columns([1.5, 1, 1.2, 1])
            activity = c1.selectbox("Attività", ["Riposo", "Corsa", "Bici", "Altro"], key=f"{day}_act", label_visibility="collapsed")
            duration = c2.number_input("Minuti", 0, 300, 0, key=f"{day}_dur", label_visibility="collapsed") if activity != "Riposo" else 0
            intensity = c3.selectbox("Intensità", ["Bassa (Z1-Z2)", "Media (Z3)", "Alta (Z4+)"], key=f"{day}_int", label_visibility="collapsed") if activity != "Riposo" else "Riposo"
            cho_in = c4.number_input("CHO (g)", 0, 1500, 300, key=f"{day}_cho", label_visibility="collapsed", help="Carboidrati totali assunti nella giornata")
            
            weekly_schedule.append({
                "day": day, "activity": activity, "duration": duration, 
                "intensity": intensity, "cho_in": cho_in
            })
            
        if st.button("Calcola Trend Settimanale"):
            initial_muscle = st.session_state['base_tank_data']['max_capacity_g'] - 100 
            initial_liver = 100
            max_muscle = st.session_state['base_tank_data']['max_capacity_g'] - 100
            max_liver = 120 
            vo2max_calc = st.session_state['base_subject_struct'].vo2max_absolute_l_min * 1000 / weight 

            df_weekly = calculate_weekly_balance(initial_muscle, initial_liver, max_muscle, max_liver, weekly_schedule, weight, vo2max_calc)
            
            st.markdown("### 📉 Andamento Riserve Glicogeno (7 Giorni)")
            
            chart_weekly_line = alt.Chart(df_weekly).mark_line(point=True).encode(
                x=alt.X('Giorno', sort=days),
                y=alt.Y('Totale', title='Glicogeno Totale (g)'),
                tooltip=['Giorno', 'Totale', 'Glicogeno Muscolare', 'Glicogeno Epatico', 'Allenamento', 'CHO In']
            ).properties(height=300)
            
            st.altair_chart(chart_weekly_line, use_container_width=True)


# --- TAB 3: SIMULAZIONE & STRATEGIA ---
with tab3:
    if 'tank_g' not in st.session_state:
        st.warning("Completare prima i Tab '1. Profilo Base & Capacità' e '2. Stato Pre-Evento (Riempimento)'.")
        st.stop()
    else:
        # Recupero i dati completi
        tank_data = st.session_state['tank_data']
        start_tank = tank_data['actual_available_g']
        subj = st.session_state.get('subject_struct', None)
        
        # Recupero i dati di soglia dal Tab 1
        ftp_watts = st.session_state.get('ftp_watts_input', 250)
        thr_hr = st.session_state.get('thr_hr_input', 170)
        max_hr = st.session_state.get('max_hr_input', 185)
        
        
        sport_mode = 'cycling'
        if subj.sport == SportType.RUNNING:
            sport_mode = 'running'
        elif subj.sport in [SportType.SWIMMING, SportType.XC_SKIING, SportType.TRIATHLON]:
            sport_mode = 'other' 
            
        col_param, col_meta = st.columns([1, 1])
        
        act_params = {'mode': sport_mode}
        duration = 120 # Default
        cho_per_unit = 25 # Default
        carb_intake = 60  # Default
        
        # Inizializzazioni per la lettura del file
        avg_w = 200
        avg_hr = 150
        intensity_series = None # Inizializzazione della serie IF
        intensity_factor_reference = 0.8 # Inizializzazione IF di riferimento
        
        if sport_mode == 'cycling':
            avg_w = 200
            intensity_factor_reference = avg_w / ftp_watts if ftp_watts > 0 else 0.8
        elif sport_mode == 'running':
            avg_hr = 150
            intensity_factor_reference = avg_hr / thr_hr if thr_hr > 0 else 0.8
        elif sport_mode == 'other':
            avg_hr = 150
            intensity_factor_reference = avg_hr / max_hr if max_hr > 0 else 0.8

        
        with col_param:
            st.subheader(f"1. Parametri Sforzo ({sport_mode.capitalize()})")
            
            # NUOVA LOGICA: CARICAMENTO FILE O INSERIMENTO MANUALE
            st.markdown("#### Caratteristiche dell'Attività")
            
            file_upload_method = st.radio(
                "Fonte dati attività:", 
                ["Manuale (Media)", "Carica File Strutturato (.zwo / .fit / .gpx / .csv)"],
                key='file_upload_method'
            )
            
            if file_upload_method == "Carica File Strutturato (.zwo / .fit / .gpx / .csv)":
                st.info("I file .gpx/.fit/.csv devono contenere le colonne 'power' o 'heart_rate' per l'estrazione. I file .zwo calcolano automaticamente l'IF istantaneo.")
                uploaded_file = st.file_uploader("Carica file attività", type=['gpx', 'csv', 'fit', 'zwo'])
                
                if uploaded_file is not None:
                    try:
                        filename = uploaded_file.name
                        
                        if filename.endswith('.zwo'):
                            # Logica per ZWO (XML)
                            st.info("Analisi di un allenamento strutturato ZWO (IF istantaneo calcolato).")
                            intensity_series, duration, avg_w_calc, avg_hr_calc = parse_zwo_file(uploaded_file, ftp_watts, thr_hr, subj.sport)
                            
                            if subj.sport == SportType.CYCLING:
                                st.success(f"Dati estratti: Potenza media: {avg_w_calc:.1f} W, Durata: {duration} min.")
                                avg_w = avg_w_calc
                            elif subj.sport == SportType.RUNNING:
                                st.success(f"Dati estratti: FC media: {avg_hr_calc:.1f} BPM, Durata: {duration} min.")
                                avg_hr = avg_hr_calc
                            
                        else:
                            # Logica per CSV/GPX/FIT (lettura semplificata in CSV)
                            df_activity = pd.read_csv(uploaded_file)
                            
                            # Simula l'estrazione di dati chiave (assumendo 5s per riga come proxy di risoluzione)
                            duration_sec = df_activity.shape[0] * 5 
                            duration = round(duration_sec / 60)
                            
                            if sport_mode == 'cycling':
                                if 'power' in df_activity.columns:
                                    avg_w = df_activity['power'].mean()
                                    st.success(f"Dati estratti: Potenza media: {avg_w:.1f} W, Durata: {duration} min.")
                                else:
                                    st.error("Il file deve contenere la colonna 'power'.")
                            
                            elif sport_mode == 'running' or sport_mode == 'other':
                                if 'heart_rate' in df_activity.columns:
                                    avg_hr = df_activity['heart_rate'].mean()
                                    st.success(f"Dati estratti: FC media: {avg_hr:.1f} BPM, Durata: {duration} min.")
                                else:
                                    st.error("Il file deve contenere la colonna 'heart_rate'.")
                            
                    except Exception as e:
                        st.error(f"Errore nell'elaborazione del file: {e}")
                        
            # --- INPUT MANUALE / RIEPILOGO DATI ---
            if sport_mode == 'cycling':
                avg_w = st.number_input("Potenza Media Prevista [Watt]", 50, 600, int(avg_w), step=5)
                act_params['ftp_watts'] = ftp_watts
                act_params['avg_watts'] = avg_w
                act_params['efficiency'] = st.slider("Efficienza Meccanica [%]", 16.0, 26.0, 22.0, 0.5)
                duration = st.slider("Durata Attività (min)", 30, 420, int(duration), step=10)
                
                # Calcola IF di riferimento
                intensity_factor_reference = avg_w / ftp_watts if ftp_watts > 0 else 0.8

            elif sport_mode == 'running':
                run_input_mode = st.radio("Modalità Obiettivo:", ["Imposta Passo & Distanza", "Imposta Tempo & Distanza"], horizontal=True)
                c_dist, c_var = st.columns(2)
                distance_km = c_dist.number_input("Distanza (km)", 1.0, 100.0, 21.1, 0.1)
                paces_options = []
                for m in range(2, 16): 
                    for s in range(0, 60, 5):
                        paces_options.append(f"{m}:{s:02d}")

                if run_input_mode == "Imposta Passo & Distanza":
                    pace_str = c_var.select_slider("Passo Obiettivo (min/km)", options=paces_options, value="5:00")
                    pm, ps = map(int, pace_str.split(':'))
                    pace_decimal = pm + ps/60.0
                    duration = distance_km * pace_decimal
                    speed_kmh = 60.0 / pace_decimal
                    st.info(f"Tempo Stimato: **{int(duration // 60)}h {int(duration % 60)}m**")
                else:
                    target_h = c_var.number_input("Ore", 0, 24, 1)
                    target_m = c_var.number_input("Minuti", 0, 59, 45)
                    duration = (target_h * 60) + target_m
                    if duration == 0: duration = 1
                    pace_decimal = duration / distance_km
                    speed_kmh = 60.0 / pace_decimal
                    p_min = int(pace_decimal)
                    p_sec = int((pace_decimal - p_min) * 60)
                    st.info(f"Passo Richiesto: **{p_min}:{p_sec:02d} /km**")

                act_params['speed_kmh'] = speed_kmh
                
                avg_hr = st.number_input("Frequenza Cardiaca Media", 80, 220, int(avg_hr), 1)
                act_params['avg_hr'] = avg_hr
                act_params['threshold_hr'] = thr_hr
                
                # Calcola IF di riferimento
                intensity_factor_reference = avg_hr / thr_hr if thr_hr > 0 else 0.8
                
            else: 
                avg_hr = st.number_input("Frequenza Cardiaca Media Gara", 80, 220, int(avg_hr), 1)
                act_params['avg_hr'] = avg_hr
                act_params['max_hr'] = max_hr
                duration = st.slider("Durata Attività (min)", 30, 420, int(duration), step=10)
                
                # Calcola IF di riferimento (usiamo max HR come soglia)
                intensity_factor_reference = avg_hr / max_hr if max_hr > 0 else 0.8
            
        with col_meta:
            st.subheader("2. Strategia di Integrazione e Calibrazione")
            
            # NUTRIZIONE PRATICA
            st.subheader("Gestione Nutrizione Pratica")
            cho_per_unit = st.number_input("Contenuto CHO per Gel/Barretta (g)", 10, 100, 25, 5, help="Es. Un gel isotonico standard ha circa 22g, uno 'high carb' 40g.")
            carb_intake = st.slider("Target Integrazione (g/h)", 0, 120, 60, step=10, help="Quantità media di CHO da assumere ogni ora.")
            
            if carb_intake > 0 and cho_per_unit > 0:
                units_per_hour = carb_intake / cho_per_unit
                if units_per_hour > 0:
                    interval_min = 60 / units_per_hour
                    st.caption(f"Protocollo: {units_per_hour:.1f} unità/h (1 ogni **{int(interval_min)} min**)")
            
            # NUOVO SELETTORE MIX CHO
            mix_type_options = list(ChoMixType)
            selected_mix_type = st.selectbox(
                "Tipologia Mix Carboidrati", 
                options=mix_type_options, 
                format_func=lambda x: x.label,
                index=0,
                help="Il tipo di carboidrati influenza il tasso massimo di ossidazione esogena."
            )

            st.markdown("---")

            use_lab = st.checkbox("Usa Dati Reali da Metabolimetro (Test)", help="Se hai fatto un test del gas in laboratorio, inserisci i dati reali per la massima precisione.")
            act_params['use_lab_data'] = use_lab
            
            if use_lab:
                st.info("Inserisci i consumi misurati al **Ritmo Gara** previsto.")
                lab_cho = st.number_input("Consumo CHO (g/h) da Test", 0, 400, 180, 5)
                lab_fat = st.number_input("Consumo Grassi (g/h) da Test", 0, 150, 30, 5)
                act_params['lab_cho_g_h'] = lab_cho
                act_params['lab_fat_g_h'] = lab_fat
                crossover = 75 
            else:
                crossover = st.slider("Crossover Point (Soglia Aerobica) [% Soglia]", 50, 85, 70, 5,
                                      help="Punto in cui il consumo di grassi e carboidrati è equivalente (RER ~0.85).")
                if crossover > 75: st.caption("Profilo: Alta efficienza lipolitica (Diesel)")
                elif crossover < 60: st.caption("Profilo: Prevalenza glicolitica (Turbo)")
                else: st.caption("Profilo: Bilanciato / Misto")
            
            st.markdown("---")
            st.subheader("3. Calibrazione Fisiologica (Utenti Esperti)")

            # CHECKBOX PER PARAMETRI AVANZATI
            use_custom_kinetic = st.checkbox(
                "Usa parametri cinetici/fisiologici personalizzati",
                help="Attiva questa opzione per calibrare τ (assorbimento), Rischio GI, Efficienza Ossidativa e Picco Ossidazione.",
                value=False
            )
            
            TAU_DEFAULT = 20.0
            RISK_THRESHOLD_DEFAULT = 30
            EFFICIENCY_DEFAULT = 0.80
            
            tau_absorption_input = TAU_DEFAULT
            risk_threshold_input = RISK_THRESHOLD_DEFAULT
            oxidation_efficiency_input = EFFICIENCY_DEFAULT
            custom_max_exo_rate = None 

            if use_custom_kinetic:
                col_tau, col_risk = st.columns(2)
                with col_tau:
                    tau_absorption_input = st.slider(
                        "Tau (τ) Cinetica Assorbimento (min)", 
                        5.0, 60.0, TAU_DEFAULT, 2.5, 
                        help="Tempo di 'smussamento'. Minore è il valore, più veloce è l'assorbimento."
                    )
                with col_risk:
                    risk_threshold_input = st.slider(
                        "Soglia di Rischio GI (g)", 
                        10, 80, RISK_THRESHOLD_DEFAULT, 5, 
                        help="Massimo accumulo tollerabile prima che insorgano sintomi GI."
                    )
                
                st.markdown("#### Fisiologia Ossidativa")
                oxidation_efficiency_input = st.slider(
                    "Efficienza di Ossidazione (%)",
                    0.50, 1.00, EFFICIENCY_DEFAULT, 0.01,
                    format="%.2f",
                    help="Percentuale di CHO ingeriti che viene effettivamente ossidata (Podlogar et al., 2025: 58-83%)."
                )
                
                use_manual_peak = st.checkbox("Inserisci manualmente il Picco Ossidazione Esogena (g/min)")
                if use_manual_peak:
                    custom_max_exo_rate = st.slider(
                        "Picco Ossidazione Esogena (g/min)",
                        0.5, 2.0, 1.0, 0.1,
                        help="Massimo tasso di ossidazione di glucosio esogeno (Standard: ~1.0 g/min)."
                    )

            else:
                 st.caption(f"Utilizzo parametri standard: τ={TAU_DEFAULT:.0f}m, Rischio={RISK_THRESHOLD_DEFAULT}g, Eff={EFFICIENCY_DEFAULT*100:.0f}%")


        h_cm = subj.height_cm 
        
        df_sim, stats = simulate_metabolism(
            tank_data, duration, carb_intake, cho_per_unit, crossover, 
            tau_absorption_input, subj, act_params,
            oxidation_efficiency_input=oxidation_efficiency_input,
            custom_max_exo_rate=custom_max_exo_rate,
            mix_type_input=selected_mix_type,
            intensity_series=intensity_series # Passa la serie IF istantanea
        )
        df_sim["Scenario"] = "Con Integrazione (Strategia)"
        
        df_no_cho, stats_no_cho = simulate_metabolism(
            tank_data, duration, 0, cho_per_unit, crossover, 
            tau_absorption_input, subj, act_params,
            oxidation_efficiency_input=oxidation_efficiency_input,
            custom_max_exo_rate=custom_max_exo_rate,
            mix_type_input=selected_mix_type,
            intensity_series=intensity_series # Passa la serie IF istantanea
        )
        df_no_cho["Scenario"] = "Senza Integrazione (Digiuno)"
        
        combined_df = pd.concat([df_sim, df_no_cho])
        
        st.markdown("---")
        st.subheader("Analisi Cinetica e Substrati")
        
        c_if, c_rer, c_mix, c_res = st.columns(4)
        
        if_val = stats['intensity_factor']
        c_if.metric("Intensity Factor (IF)", f"{if_val:.2f}", help="Indice di intensità normalizzato sulla soglia.")
        
        rer_val = stats['avg_rer']
        c_rer.metric("RER Stimato (RQ)", f"{rer_val:.2f}", help="Quoziente Respiratorio Metabolico.")
        
        c_mix.metric("Ripartizione Substrati", f"{int(stats['cho_pct'])}% CHO",
                      delta=f"{100-int(stats['cho_pct'])}% FAT", delta_color="off")
        
        c_res.metric("Glicogeno Residuo", f"{int(stats['final_glycogen'])} g", 
                      delta=f"{int(stats['final_glycogen'] - start_tank)} g")

        st.markdown("---")
        
        m1, m2, m3 = st.columns(3)
        m1.metric("Uso Glicogeno Muscolare", f"{int(stats['total_muscle_used'])} g", help="Totale svuotato dalle gambe")
        m2.metric("Uso Glicogeno Epatico", f"{int(stats['total_liver_used'])} g", help="Totale prelevato dal fegato")
        m3.metric("Uso CHO Esogeno", f"{int(stats['total_exo_used'])} g", help="Totale energia da integrazione")

        
        st.markdown("### 📊 Bilancio Energetico: Richiesta vs. Fonti di Ossidazione")
        
        # Didascalia Esplicativa Aggiunta
        st.caption("""
        **Guida alla Lettura:** L'altezza totale del grafico (linea tratteggiata nera) rappresenta il consumo energetico orario (g/h) richiesto dallo sforzo. Le aree colorate mostrano come il corpo miscela i diversi substrati per soddisfare esattamente quella richiesta.
        """)

        color_map = {
            'Glicogeno Epatico (g)': '#B71C1C',    # Rosso Scuro (1) - BASE
            'Carboidrati Esogeni (g)': '#1976D2', # Blu (2)
            'Ossidazione Lipidica (g)': '#FFC107', # Giallo Intenso (3)
            'Glicogeno Muscolare (g)': '#E57373', # Rosso Tenue (4) - CIMA
        }
        
        stack_order = [
            'Glicogeno Epatico (g)',     # 1. BASE (indice 0)
            'Carboidrati Esogeni (g)',   # 2. Sopra 1 (indice 1)
            'Ossidazione Lipidica (g)',  # 3. Sopra 2 (indice 2)
            'Glicogeno Muscolare (g)'      # 4. CIMA (indice 3)
        ]
        
        df_long = df_sim.melt('Time (min)', value_vars=stack_order, 
                              var_name='Source', value_name='Rate (g/h)')
        
        df_long_rich = pd.merge(df_long, df_sim[['Time (min)', 'Pct_Muscle', 'Pct_Liver', 'Pct_Exo', 'Pct_Fat', 'Scenario']], on='Time (min)')
        
        conditions = [
            (df_long_rich['Source'] == 'Glicogeno Muscolare (g)'),
            (df_long_rich['Source'] == 'Glicogeno Epatico (g)'),
            (df_long_rich['Source'] == 'Carboidrati Esogeni (g)'),
            (df_long_rich['Source'] == 'Ossidazione Lipidica (g)')
        ]
        choices = [df_long_rich['Pct_Muscle'], df_long_rich['Pct_Liver'], df_long_rich['Pct_Exo'], df_long_rich['Pct_Fat']]
        df_long_rich['Percentuale'] = np.select(conditions, choices, default='0%')

        
        sort_map = {
            'Glicogeno Epatico (g)': 0,
            'Carboidrati Esogeni (g)': 1,
            'Ossidazione Lipidica (g)': 2,
            'Glicogeno Muscolare (g)': 3
        }
        df_long_rich['sort_index'] = df_long_rich['Source'].map(sort_map)
        
        color_domain = stack_order
        color_range = [color_map[source] for source in stack_order]
        
        df_total_demand = df_sim.copy()
        df_total_demand['Total Demand'] = df_total_demand['Glicogeno Muscolare (g)'] + df_total_demand['Glicogeno Epatico (g)'] + df_total_demand['Carboidrati Esogeni (g)'] + df_total_demand['Ossidazione Lipidica (g)']

        chart_stack = alt.Chart(df_long_rich).mark_area().encode(
            x=alt.X('Time (min)'),
            y=alt.Y('Rate (g/h)', stack="zero"), # Usiamo stack="zero" per maggiore chiarezza
            color=alt.Color('Source', 
                            scale=alt.Scale(domain=color_domain,  
                                            range=color_range),
                            sort=alt.SortField(field='sort_index', order='ascending') 
                           ),
            tooltip=[
                alt.Tooltip('Time (min)', title='Minuto'), 
                alt.Tooltip('Source', title='Fonte'), 
                alt.Tooltip('Rate (g/h)', title='Contributo (g/h)', format='.1f'),
                alt.Tooltip('Percentuale', title='% del Totale')
            ]
        )
        
        line_demand = alt.Chart(df_total_demand).mark_line(color='black', strokeDash=[5,5], size=2).encode(
            x='Time (min)',
            y='Total Demand'
        )

        final_combo_chart = (chart_stack + line_demand).properties(
            title="Bilancio Energetico: Richiesta vs. Fonti di Ossidazione" 
        ).interactive()
        
        st.altair_chart(final_combo_chart, use_container_width=True)
        
        st.markdown("---")
        st.markdown("### 📉 Confronto Riserve Nette (Svuotamento Serbatio)")
        
        st.caption("Confronto: Deplezione Glicogeno Totale (Muscolo + Fegato) con Zone di Rischio")
        
        # --- LOGICA PER GRAFICO CON BANDE DI RISCHIO BASATO SU TOTALE GLICOGENO ---
        
        initial_total_glycogen = tank_data['muscle_glycogen_g'] + tank_data['liver_glycogen_g']
        max_total = initial_total_glycogen * 1.05 # Max per l'asse Y
        
        # Definisce i campi da mostrare nel grafico a pila delle riserve
        reserve_fields = ['Residuo Muscolare', 'Residuo Epatico']

        # Melt dei dati per la visualizzazione stacked
        df_reserve_long = combined_df.melt(
            id_vars=['Time (min)', 'Scenario', 'Stato'], 
            value_vars=reserve_fields, 
            var_name='Tipo Glicogeno', 
            value_name='Residuo (g)'
        )
        
        # Mappatura colori specifica per le riserve (chiaro per Muscolo, scuro per Fegato critico)
        reserve_color_map = {
            'Residuo Muscolare': '#E57373', # Rosso tenue
            'Residuo Epatico': '#B71C1C',   # Rosso scuro/critico
        }
        
        # 1. Definizione delle zone di rischio (Basato su Riserva Totale)
        zones_df = pd.DataFrame({
            'Zone': ['Sicurezza (Verde)', 'Warning (Giallo)', 'Critico (Rosso)'],
            'Start': [initial_total_glycogen * 0.65, initial_total_glycogen * 0.30, 0],
            'End': [initial_total_glycogen * 1.05, initial_total_glycogen * 0.65, initial_total_glycogen * 0.30],
            'Color': ['#4CAF50', '#FFC107', '#F44336'], 
        })
        
        # Creazione dei grafici affiancati utilizzando la divisione dei dati e la combinazione dei layer
        
        col_strat, col_digi = st.columns(2)

        def create_reserve_chart(df_data, title, background_df):
            
            # Layer Sfondo
            background = alt.Chart(background_df).mark_rect(opacity=0.15).encode(
                y=alt.Y('Start', title='Glicogeno Residuo (g)', axis=None),
                y2=alt.Y2('End'),         
                color=alt.Color('Color', scale=None), 
                tooltip=['Zone']
            ).properties(
                title=title
            )

            # Layer Area Accatastata
            area_chart = alt.Chart(df_data).mark_area().encode(
                x=alt.X('Time (min)', title='Durata (min)'),
                y=alt.Y('Residuo (g)', title='Glicogeno Residuo (g)', stack="zero", scale=alt.Scale(domain=[0, max_total])),
                color=alt.Color('Tipo Glicogeno', scale=alt.Scale(domain=reserve_fields, range=[reserve_color_map[f] for f in reserve_fields])),
                order=alt.Order('Tipo Glicogeno', sort='ascending'), # Epatico in basso, Muscolare sopra
                tooltip=['Time (min)', 'Tipo Glicogeno', 'Residuo (g)', 'Stato']
            ).interactive()
            
            return alt.layer(background, area_chart).properties(height=350)
            
        # Grafico 1: Strategia con Integrazione
        df_strat = df_reserve_long[df_reserve_long['Scenario'] == 'Con Integrazione (Strategia)']
        chart_strat = create_reserve_chart(df_strat, 'Con Integrazione (Strategia)', zones_df)
        
        with col_strat:
            st.altair_chart(chart_strat, use_container_width=True)

        # Grafico 2: Senza Integrazione
        df_digi = df_reserve_long[df_reserve_long['Scenario'] == 'Senza Integrazione (Digiuno)']
        chart_digi = create_reserve_chart(df_digi, 'Senza Integrazione (Digiuno)', zones_df)
        
        with col_digi:
            st.altair_chart(chart_digi, use_container_width=True)
            
        st.markdown(f"""
        <p style='text-align: center; font-size: small; color: #666;'>
        Il Glicogeno Epatico (<span style='color: #B71C1C;'>Rosso Scuro</span>) è alla base per evidenziare il rischio di Ipoglicemia (crisi del fegato).
        </p>
        """, unsafe_allow_html=True)
        # --- FINE LOGICA GRAFICO RISERVE NETTE ---
        
        st.markdown("---")
        
        st.markdown("### ⚠️ Accumulo Intestinale (Rischio GI) & Flusso CHO")
        
        st.caption(f"""
        **Interpretazione:** La distanza verticale tra la Linea Blu (Ingerito) e la Linea Verde (Ossidato) crea l'**Accumulo CHO (g)**, ovvero il carico intestinale istantaneo. Se l'area supera la Soglia di Rischio GI ({risk_threshold_input} g), la strategia di assunzione è troppo aggressiva.
        """)

        with st.expander("Dettagli Modello Flusso CHO e Rischio GI"):
            st.markdown(f"""
            Questo grafico visualizza il **bilancio dinamico** tra ciò che ingerisci e ciò che il tuo corpo riesce ad ossidare (bruciare), indicando il rischio di *Distress Gastrointestinale (GI)*.
            
            **Linee Cumulative (Asse Destro):**
            * **Linea Blu (Intake):** Apporto totale di CHO (a gradini, riflette le assunzioni discrete).
            * **Linea Verde (Ossidazione):** CHO totale bruciato (curva smussata, limitata dalla cinetica di assorbimento).
            
            **Area di Rischio (Asse Sinistro):**
            * L'area sottesa è l'**Accumulo Intestinale (Gut Load)**: $\\text{{Intake}} - \\text{{Ossidazione}}$.
            * **τ Cinetica (Tempo di Smussamento):** {tau_absorption_input:.1f} min. Determina quanto velocemente la curva di Ossidazione (Verde) risponde all'Ingestione (Blu).
            * **Soglia di Rischio GI:** {risk_threshold_input} g (Linea Rossa Tratteggiata). Superarla indica un alto rischio di sintomi GI.
            """)
        
        RISK_THRESHOLD = risk_threshold_input
        
        df_sim['Rischio'] = np.where(df_sim['Gut Load'] >= RISK_THRESHOLD, 'Alto Rischio', 'Basso Rischio')
        
        max_gut_load = df_sim['Gut Load'].max()
        max_gut_load_time = df_sim[df_sim['Gut Load'] == max_gut_load]['Time (min)'].iloc[0] if max_gut_load > 0 else 0
        max_df = pd.DataFrame([{'Time (min)': max_gut_load_time, 'Gut Load': max_gut_load}])

        gut_area = alt.Chart(df_sim).mark_area(opacity=0.8, color='#8D6E63').encode(
            x=alt.X('Time (min)'), 
            y=alt.Y('Gut Load', title='Accumulo CHO (g)', axis=alt.Axis(titleColor='#8D6E63')),
            tooltip=['Time (min)', 'Gut Load', 'Rischio']
        )
        
        risk_line = alt.Chart(pd.DataFrame({'y': [RISK_THRESHOLD]})).mark_rule(color='#F44336', strokeDash=[4,4], size=2).encode(
            y=alt.Y('y', axis=None)
        )
        
        max_point = alt.Chart(max_df).mark_circle(size=80, color='black').encode(
            x=alt.X('Time (min)'), 
            y=alt.Y('Gut Load'),
            tooltip=[alt.Tooltip('Time (min)', title='Max Time'), alt.Tooltip('Gut Load', title='Max Accumulo')]
        )
        
        gut_layer_base = alt.layer(gut_area, risk_line, max_point)

        df_cumulative = df_sim.melt('Time (min)', value_vars=['Intake Cumulativo (g)', 'Ossidazione Cumulativa (g)'],
                                   var_name='Flusso', value_name='Grammi')

        intake_oxidation_lines = alt.Chart(df_cumulative).mark_line(strokeWidth=3.5).encode(
            x=alt.X('Time (min)'), 
            y=alt.Y('Grammi', title='G Ingeriti/Ossidati (g)', axis=alt.Axis(titleColor='#1976D2')),
            color=alt.Color('Flusso', 
                            scale=alt.Scale(domain=['Intake Cumulativo (g)', 'Ossidazione Cumulativa (g)'],
                                            range=['#1976D2', '#4CAF50'])
                           ),
            strokeDash=alt.condition(alt.datum.Flusso == 'Ossidazione Cumulativa (g)', alt.value([5, 5]), alt.value([0])),
            tooltip=['Time (min)', 'Flusso', 'Grammi']
        )

        cumulative_layer = intake_oxidation_lines.encode(
            y=alt.Y('Grammi', 
                    axis=alt.Axis(title='G Ingeriti/Ossidati (g)', titleColor='#1976D2', orient='right'), 
                    scale=alt.Scale(domain=[0, df_sim['Intake Cumulativo (g)'].max() * 1.1])
                    )
        )
        
        final_gut_chart = alt.layer(
            gut_layer_base,
            cumulative_layer
        ).resolve_scale(
            y='independent'
        ).properties(
            title="Accumulo Intestinale vs Flusso CHO (Doppio Asse Y)"
        )


        st.altair_chart(final_gut_chart, use_container_width=True)
        
        st.caption("Ossidazione Lipidica (Tasso Orario)")
        st.line_chart(df_sim.set_index("Time (min)")["Ossidazione Lipidica (g)"], color="#FFA500")
        
        st.markdown("---")
        
        st.subheader("Strategia & Timing")
        
        liver_bonk_time = df_sim[df_sim['Residuo Epatico'] <= 0]['Time (min)'].min()
        muscle_bonk_time = df_sim[df_sim['Residuo Muscolare'] <= 20]['Time (min)'].min()
        bonk_time = min(filter(lambda x: not np.isnan(x), [liver_bonk_time, muscle_bonk_time]), default=None)
        
        s1, s2 = st.columns([2, 1])
        with s1:
            if bonk_time:
                st.error(f"CRITICITÀ RILEVATA AL MINUTO {int(bonk_time)}")
                if not np.isnan(liver_bonk_time) and liver_bonk_time == bonk_time:
                    st.write("Causa Primaria: **Esaurimento Glicogeno Epatico (Ipoglicemia)**.")
                else:
                    st.write("Causa Primaria: **Esaurimento Glicogeno Muscolare**.")
            else:
                st.success("STRATEGIA SOSTENIBILE")
                st.write("Il bilancio energetico stimato consente di completare la prova senza deplezione critica.")
        
        with s2:
            if bonk_time:
                st.metric("Tempo Limite Stimato", f"{int(bonk_time)} min", delta_color="inverse")
            else:
                st.metric("Buffer Energetico", "Adeguato")
        
        st.markdown("### 📋 Cronotabella di Integrazione")
        
        if carb_intake > 0 and cho_per_unit > 0:
            units_per_hour = carb_intake / cho_per_unit
            if units_per_hour > 0:
                interval_min = 60 / units_per_hour
                interval_int = int(interval_min)
                
                schedule = []
                current_time = interval_int
                total_cho_ingested = 0
                
                while current_time <= duration:
                    total_cho_ingested += cho_per_unit
                    schedule.append({
                        "Minuto": current_time,
                        "Azione": f"Assumere 1 unità ({cho_per_unit}g CHO)",
                        "Totale Ingerito": f"{total_cho_ingested}g"
                    })
                    current_time += interval_int
                
                if schedule:
                    st.table(pd.DataFrame(schedule))
                else:
                    st.info("Durata troppo breve per l'intervallo di assunzione calcolato.")
            else:
                st.warning("Verificare i parametri di integrazione.")
        else:
            st.info("Nessuna integrazione pianificata.")
