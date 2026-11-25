(* I/O mapping probe. Deploy with tools/deploy.py, then run tools/io_probe.py.
   Outputs fixed bit patterns and loopbacks so the Modbus side can see exactly
   where each located variable appears and how 16-bit values are ordered. *)

PROGRAM io_probe
  VAR
    clock : TON;
    windows : DINT;
  END_VAR
  (* located variables must sit in their own VAR block on this compiler *)
  VAR
    q0  AT %QX0.0 : BOOL;
    q1  AT %QX0.1 : BOOL;
    q7  AT %QX0.7 : BOOL;
    q8  AT %QX1.0 : BOOL;
    q9  AT %QX1.1 : BOOL;
    qw0 AT %QW0 : INT;
    qw1 AT %QW1 : INT;
    qw2 AT %QW2 : INT;
    qw3 AT %QW3 : INT;
    qw4 AT %QW4 : INT;
    mw0   AT %MW0   : INT;
    mw1   AT %MW1   : INT;
    mw100 AT %MW100 : INT;
  END_VAR

  (* fixed pattern: reveals the %QX -> coil mapping, one address per bit *)
  q0 := TRUE;
  q1 := FALSE;
  q7 := TRUE;
  q8 := TRUE;
  q9 := FALSE;

  (* loopbacks: reveal the %MW -> holding register mapping and the word order *)
  qw0 := mw0;
  qw1 := mw0 + 1;
  qw2 := mw100;
  qw3 := mw1;

  (* one pulse every nominal 10 s so the Modbus side can time it against a wall clock *)
  clock(IN := NOT clock.Q, PT := T#10s);
  IF clock.Q THEN
    windows := windows + 1;
  END_IF;
  qw4 := DINT_TO_INT(windows);

END_PROGRAM


CONFIGURATION Config0

  RESOURCE Res0 ON PLC
    TASK Main(INTERVAL := T#50ms, PRIORITY := 0);
    PROGRAM Inst0 WITH Main : io_probe;
  END_RESOURCE

END_CONFIGURATION
