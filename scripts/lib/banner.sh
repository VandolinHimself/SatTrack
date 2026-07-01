#!/usr/bin/env bash
# SatTrack install banner — flashy intro text (ANSI colors when supported).

print_sattrack_banner() {
  local reset bold cyan magenta yellow blue dim
  if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    reset="$(tput sgr0 2>/dev/null || echo '')"
    bold="$(tput bold 2>/dev/null || echo '')"
    cyan="$(tput setaf 6 2>/dev/null || echo '')"
    magenta="$(tput setaf 5 2>/dev/null || echo '')"
    yellow="$(tput setaf 3 2>/dev/null || echo '')"
    blue="$(tput setaf 4 2>/dev/null || echo '')"
    dim="$(tput dim 2>/dev/null || echo '')"
  else
    reset=; bold=; cyan=; magenta=; yellow=; blue=; dim=
  fi

  echo
  while IFS= read -r line || [[ -n "$line" ]]; do
    echo "${cyan}${bold}${line}${reset}"
  done <<'BANNER'
gggQQQQQQQQQQQQQgQQgQggQggQgggQQQQQQQQQQQQQQQQQQQggggggggggggQQQQQgggQggggggggQQQg
QQQQQQQQQQQQQQQQQQQQQQQQQQQQgQQQQQQQQQQQQQ%4QQQQQggggggQggggQgQgQQQggQQggggMggggQg
QQQ .QQQQQQQQQQQQQQQQQ vQQQQQQQQQQQQQQQQQ\.! yQgggggggggggQgggQQQQQgQggggggggggggg
QQQ .QQQQwaQQyRQQySAQQeRQyRQRy%QRey8QQQQQ LQ'|QQQggggggggQgggggQQQMQQggggggggggggg
QQQ .QQQQ' Qg v$.:< ?Q vQ vQv HS c! #QQQQI :,#QQgQggQggggggQgggQgQMQQQgggggggggggg
QQQ .QQQQ' QQ v7 RQdaQ vQ vQv Hl LyQQQQQO': v8 AQgggggggggQggggggQQQQggggggggggggg
QQQ .QQQQ' Qg vv 8QQQQ vQ vQv AQAv: bQQQ^ QO..'QQgggggggggggggggQQQMgggQQggQQggggg
QQQ .yyyR! eL vA \S'"Q vQ ^5! Oc Zo YQQQr ny^ :WQggQgggggggQgQQggQQgQgQggQgggggggg
QQQ<<<<<aR\>k<yQO<<vNQ<yQR!<I<DQ7<<oQQQQQf!<sN<vQQgggggQggggggggQQQQggQQgggggggggg
QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQggggggggggggggggQQgQgggggggggggg
QQp<aQP;yQ4<qQQQQQQQQQQQQQQQQQQQQQQQQQQv"'<RQQQQQQQQQQQQ8AQgQggggggggggggggggggggg
QQQ cQv ?Qv QQQQQQQQQQQQQQQQQQQQQQQQQQv AM^ QQQQQQQQQQQQL QQQQQQQgQgQQgQQgggQQgggg
QQQ" Q:. Q^,g!.':%'^Qo R :. *QS.,.SQQQt vNQQw PQ:^4'^ fv   QL ':Rv : '\  aQ,:.>ggg
QQQv M R D v%j%Q vz Q\,Q \Qv Q LQL QQQQ{^ :aQ'|M 5v.HRaQ) QD PQ vv %M vQ.Lk hNaQgg
QQQN , Q:! #R:,< vQ i IQ vQv Q :<<<QQQDaQM< Qi 7.QNL^ LQL Qw ^<<av gQ vQ.vQn! ^Qgg
QQQQ. \Qv  Qv DO vQL' QQ vQv Q'>Qv?QQQT 5b! QQ 'vg2|Q* Qv 6% TN!av QQ vQ.LV<DA Ogg
QQQQy;yQw>5gQ?<v<yQN LQQ<yQy<QR*<<RQQQQs><*RQQ| QgR\<!RQR!!QP!<LQy<gQ<yQ\ygv><iggg
QggQQQQQQQQQQQQQQQV".MQQQQQQQQQQQQQQQQQQQQQQQ*'?QQQQQQQQQQQQQQQQQQQQQQQQQggggggggg
QQgQQQQQQQQQQQQQQQRyMQQQQQQQQQQQQQQQQQQQQQQQQabQQQQQQQQQQQQQQQQQQQQggggggggggggggg
QQQgQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQggggggggggggggg
BANNER

  cat <<TAGLINE

${magenta}${bold}  ╔══════════════════════════════════════════════════════════════╗
  ║${reset}  ${yellow}★${reset}  ${bold}AUTOMATED SATELLITE GROUND STATION${reset}  ${yellow}★${reset}                        ${magenta}${bold}║
  ║${reset}     Kismet ADS-B  ${dim}+${reset}  SatDump live decode  ${dim}+${reset}  RTL-SDR scheduling   ${magenta}${bold}║
  ╚══════════════════════════════════════════════════════════════╝${reset}

${blue}  ┌─${reset} Point antenna at sky. Plug in dongle. Answer a few questions. Done.
${blue}  └─${reset} ${dim}No manual config.json editing required.${reset}

TAGLINE
  echo
}
