(*
  AHU-01 control program for OpenPLC (IEC 61131-3 Structured Text).

  Implements docs/control-narrative.md: interlocked start/stop sequencing, PI control of
  supply-air temperature through the chilled-water valve, CO2 demand-controlled ventilation,
  and trips with latched first-out alarms. Addresses are listed in docs/address-map.md.

  Signal conventions (fail-safe, as wired on a real panel):
    - SMK_INTLK and FS_INTLK are N/C contacts: 1 = circuit closed = healthy.
    - SF_TRIP and RF_TRIP are N/O contacts: 1 = tripped.
  Analog signals are 0-10 V, carried as 0-10000 counts; engineering scaling by span.
*)

FUNCTION_BLOCK PI_CTRL
  VAR_INPUT
    e : REAL;          (* PV - SP in engineering units *)
    kp : REAL;         (* output units per engineering unit *)
    ti : REAL;         (* integral time in s *)
    sample_s : REAL;   (* seconds since the previous call; "dt" is a reserved word here *)
    enable : BOOL;
    track : REAL;      (* output held while disabled; the integral starts from it on enable *)
    y_min : REAL := 0.0;
    y_max : REAL := 100.0;
  END_VAR
  VAR_OUTPUT
    y : REAL;
  END_VAR
  VAR
    i : REAL;          (* integral term *)
    i_next : REAL;
    y_next : REAL;
  END_VAR

  IF enable THEN
    i_next := i + kp * e * sample_s / ti;
    y_next := kp * e + i_next;
    (* anti-windup: do not integrate while saturated and the error pushes further into the limit *)
    IF NOT ((y_next > y_max AND e > 0.0) OR (y_next < y_min AND e < 0.0)) THEN
      i := LIMIT(y_min, i_next, y_max);
    END_IF;
    y := LIMIT(y_min, kp * e + i, y_max);
  ELSE
    i := track;
    y := track;
  END_IF;

END_FUNCTION_BLOCK


PROGRAM ahu_ctrl
  VAR
    (* state codes, published on %QW1 *)
    ST_IDLE : INT := 0;
    ST_DAMPERS : INT := 1;
    ST_RF : INT := 2;
    ST_SF : INT := 3;
    ST_RUN : INT := 4;
    ST_PURGE : INT := 5;
    ST_TRIPPED : INT := 6;
    ST_HAND : INT := 7;
    (* first-out trip causes *)
    FO_SF_TRIP : INT := 1;
    FO_RF_TRIP : INT := 2;
    FO_SMOKE : INT := 3;
    FO_GAS : INT := 4;
    FO_SF_FB : INT := 5;
    (* alarm word bits *)
    AB_SF_TRIP : INT := 1;
    AB_RF_TRIP : INT := 2;
    AB_SMOKE : INT := 4;
    AB_GAS : INT := 8;
    AB_SF_FB : INT := 16;
    AB_FILT_DP : INT := 32;
    AB_CO2_HIGH : INT := 64;
    AB_LO_OA : INT := 128;
    (* settings *)
    sat_sp : REAL := 13.0;        (* degC *)
    co2_high : REAL := 800.0;     (* ppm: DCV opens the dampers above this *)
    co2_low : REAL := 700.0;      (* ppm: DCV closes them below this *)
    dp_alarm : REAL := 250.0;     (* Pa *)
    kp : REAL := 6.0;             (* per cent valve per K of SAT error *)
    ti : REAL := 60.0;            (* s *)
    loop_dt : REAL := 10.0;       (* s, loop period *)
    pump_min_pct : REAL := 2.0;   (* valve demand needed to run the CHW pump *)

    state : INT;
    first_out : INT;
    dcv_open : BOOL;
    reset_edge : R_TRIG;
    t_dampers : TON;
    t_rf : TON;
    t_sf : TON;
    t_purge : TON;
    t_spindown : TON;
    t_loop : TON;
    sat_pi : PI_CTRL;

    sf_run_fb, sf_trip, rf_trip, smk_intlk, fs_intlk : BOOL;
    filt_dp_hs, lo_oa_t, man_auto, occ_sched, trip_reset : BOOL;
    sat_c, rat_c, oat_c, co2_ppm, dp_pa : REAL;
    valve_pct : REAL;
    alm_sf, alm_rf, alm_smk, alm_gas, alm_fb, alm_filt, alm_co2 : BOOL;
    sf_cmd, rf_cmd, exd_cmd, fad_cmd, pump_cmd, alm_gen : BOOL;
    alarm_w : INT;
  END_VAR
  VAR
    (* input image: holding registers 1124-1138, written by the field side *)
    di_sf_run_fb AT %MW100 : INT;
    di_sf_trip   AT %MW101 : INT;
    di_rf_trip   AT %MW102 : INT;
    di_smk_intlk AT %MW103 : INT;
    di_fs_intlk  AT %MW104 : INT;
    di_filt_dp   AT %MW105 : INT;
    di_lo_oa_t   AT %MW106 : INT;
    di_man_auto  AT %MW107 : INT;
    di_occ       AT %MW108 : INT;
    di_reset     AT %MW109 : INT;
    ai_sat AT %MW110 : INT;
    ai_rat AT %MW111 : INT;
    ai_oat AT %MW112 : INT;
    ai_co2 AT %MW113 : INT;
    ai_dp  AT %MW114 : INT;
  END_VAR
  VAR
    (* outputs: coils 0-5 and holding registers 0-2 *)
    do_sf_cmd   AT %QX0.0 : BOOL;
    do_rf_cmd   AT %QX0.1 : BOOL;
    do_exd_cmd  AT %QX0.2 : BOOL;
    do_fad_cmd  AT %QX0.3 : BOOL;
    do_alm_gen  AT %QX0.4 : BOOL;
    do_pump_cmd AT %QX0.5 : BOOL;
    ao_chwv  AT %QW0 : INT;
    ao_state AT %QW1 : INT;
    ao_alarm AT %QW2 : INT;
  END_VAR

  (* ---------- 1. Input image ---------- *)
  sf_run_fb := di_sf_run_fb <> 0;
  sf_trip := di_sf_trip <> 0;
  rf_trip := di_rf_trip <> 0;
  smk_intlk := di_smk_intlk <> 0;
  fs_intlk := di_fs_intlk <> 0;
  filt_dp_hs := di_filt_dp <> 0;
  lo_oa_t := di_lo_oa_t <> 0;
  man_auto := di_man_auto <> 0;
  occ_sched := di_occ <> 0;
  trip_reset := di_reset <> 0;
  reset_edge(CLK := trip_reset);

  sat_c := LIMIT(0.0, INT_TO_REAL(ai_sat) * 0.004, 40.0);
  rat_c := LIMIT(0.0, INT_TO_REAL(ai_rat) * 0.004, 40.0);
  oat_c := LIMIT(0.0, INT_TO_REAL(ai_oat) * 0.005, 50.0);
  co2_ppm := LIMIT(0.0, INT_TO_REAL(ai_co2) * 0.2, 2000.0);
  dp_pa := LIMIT(0.0, INT_TO_REAL(ai_dp) * 0.05, 500.0);

  (* ---------- 2. Trips: first cause only, latched until a reset with the cause cleared ---------- *)
  IF state <> ST_TRIPPED THEN
    IF sf_trip THEN
      first_out := FO_SF_TRIP;
      alm_sf := TRUE;
      state := ST_TRIPPED;
    ELSIF rf_trip THEN
      first_out := FO_RF_TRIP;
      alm_rf := TRUE;
      state := ST_TRIPPED;
    ELSIF NOT smk_intlk THEN
      first_out := FO_SMOKE;
      alm_smk := TRUE;
      state := ST_TRIPPED;
    ELSIF NOT fs_intlk THEN
      first_out := FO_GAS;
      alm_gas := TRUE;
      state := ST_TRIPPED;
    ELSIF (state = ST_SF) AND t_sf.Q AND NOT sf_run_fb THEN
      (* fan failed to prove within its start time *)
      first_out := FO_SF_FB;
      alm_fb := TRUE;
      state := ST_TRIPPED;
    ELSIF (state = ST_RUN) AND NOT sf_run_fb THEN
      (* run feedback lost while running *)
      first_out := FO_SF_FB;
      alm_fb := TRUE;
      state := ST_TRIPPED;
    END_IF;
  END_IF;

  (* ---------- 3. Sequence ---------- *)
  IF state = ST_IDLE THEN
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF occ_sched THEN
      state := ST_DAMPERS;
    END_IF;

  ELSIF state = ST_DAMPERS THEN
    (* dampers travel open before any fan is allowed to start *)
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF NOT occ_sched THEN
      state := ST_IDLE;
    ELSIF t_dampers.Q THEN
      state := ST_RF;
    END_IF;

  ELSIF state = ST_RF THEN
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF NOT occ_sched THEN
      state := ST_PURGE;
    ELSIF t_rf.Q THEN
      state := ST_SF;
    END_IF;

  ELSIF state = ST_SF THEN
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF NOT occ_sched THEN
      state := ST_PURGE;
    ELSIF t_sf.Q AND sf_run_fb THEN
      state := ST_RUN;
      dcv_open := TRUE;   (* dampers are already open from the sequence: no step on hand-over *)
    END_IF;

  ELSIF state = ST_RUN THEN
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF NOT occ_sched THEN
      state := ST_PURGE;
    END_IF;

  ELSIF state = ST_PURGE THEN
    (* supply fan off, return fan keeps clearing the duct *)
    IF NOT man_auto THEN
      state := ST_HAND;
    ELSIF t_purge.Q THEN
      state := ST_IDLE;
    END_IF;

  ELSIF state = ST_TRIPPED THEN
    IF reset_edge.Q AND NOT sf_trip AND NOT rf_trip AND smk_intlk AND fs_intlk THEN
      alm_sf := FALSE;
      alm_rf := FALSE;
      alm_smk := FALSE;
      alm_gas := FALSE;
      alm_fb := FALSE;
      first_out := 0;
      state := ST_IDLE;
    END_IF;

  ELSIF state = ST_HAND THEN
    (* Hand: automatic outputs are released; the starter is under local control *)
    IF man_auto THEN
      state := ST_IDLE;
    END_IF;
  END_IF;

  t_dampers(IN := state = ST_DAMPERS, PT := T#10s);
  t_rf(IN := state = ST_RF, PT := T#5s);
  t_sf(IN := state = ST_SF, PT := T#5s);
  t_purge(IN := state = ST_PURGE, PT := T#10s);
  t_spindown(IN := state = ST_TRIPPED, PT := T#10s);

  (* ---------- 4. SAT control loop: PI every 10 s, enabled only while running ---------- *)
  t_loop(IN := (state = ST_RUN) AND NOT t_loop.Q, PT := T#10s);
  IF state = ST_RUN THEN
    IF t_loop.Q THEN
      sat_pi(e := sat_c - sat_sp, kp := kp, ti := ti, sample_s := loop_dt,
             enable := TRUE, track := valve_pct);
    END_IF;
  ELSE
    sat_pi(enable := FALSE, track := 0.0);
  END_IF;
  valve_pct := sat_pi.y;

  (* ---------- 5. Demand-controlled ventilation with 100 ppm hysteresis ---------- *)
  IF state = ST_RUN THEN
    IF dcv_open THEN
      IF co2_ppm < co2_low THEN
        dcv_open := FALSE;
      END_IF;
    ELSIF (co2_ppm > co2_high) AND NOT lo_oa_t THEN
      dcv_open := TRUE;
    END_IF;
  END_IF;

  (* ---------- 6. Alarms ---------- *)
  alm_filt := filt_dp_hs OR (dp_pa > dp_alarm);
  alm_co2 := (state = ST_RUN) AND (co2_ppm > co2_high);
  alm_gen := alm_sf OR alm_rf OR alm_smk OR alm_gas OR alm_fb OR alm_filt;

  (* ---------- 7. Outputs ---------- *)
  sf_cmd := (state = ST_SF) OR (state = ST_RUN);
  rf_cmd := (state = ST_RF) OR (state = ST_SF) OR (state = ST_RUN) OR (state = ST_PURGE);
  fad_cmd := (state = ST_DAMPERS) OR (state = ST_RF) OR (state = ST_SF)
             OR ((state = ST_RUN) AND dcv_open);
  (* smoke purge: on a smoke trip only, the exhaust damper opens once the fan has spun down *)
  exd_cmd := fad_cmd OR ((state = ST_TRIPPED) AND (first_out = FO_SMOKE) AND t_spindown.Q);
  pump_cmd := (state = ST_RUN) AND (valve_pct > pump_min_pct);

  alarm_w := 0;
  IF alm_sf THEN
    alarm_w := alarm_w + AB_SF_TRIP;
  END_IF;
  IF alm_rf THEN
    alarm_w := alarm_w + AB_RF_TRIP;
  END_IF;
  IF alm_smk THEN
    alarm_w := alarm_w + AB_SMOKE;
  END_IF;
  IF alm_gas THEN
    alarm_w := alarm_w + AB_GAS;
  END_IF;
  IF alm_fb THEN
    alarm_w := alarm_w + AB_SF_FB;
  END_IF;
  IF alm_filt THEN
    alarm_w := alarm_w + AB_FILT_DP;
  END_IF;
  IF alm_co2 THEN
    alarm_w := alarm_w + AB_CO2_HIGH;
  END_IF;
  IF lo_oa_t THEN
    alarm_w := alarm_w + AB_LO_OA;
  END_IF;

  do_sf_cmd := sf_cmd;
  do_rf_cmd := rf_cmd;
  do_exd_cmd := exd_cmd;
  do_fad_cmd := fad_cmd;
  do_alm_gen := alm_gen;
  do_pump_cmd := pump_cmd;
  ao_chwv := REAL_TO_INT(valve_pct * 100.0);
  ao_state := state;
  ao_alarm := alarm_w;

END_PROGRAM


CONFIGURATION Config0

  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#50ms, PRIORITY := 0);
    PROGRAM Inst0 WITH Main : ahu_ctrl;
  END_RESOURCE

END_CONFIGURATION
