#!/bin/bash
# Single-file Bash + awk block copier. No Python, Bun, or eval.
# --clean reads stdin; --extract SCREEN DIRECTORY writes numbered blocks.
set -e

clean() {
awk '
function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
function known(s, a,n,i) {
 n=split("sudo ./ ~/ apt brew git python pip pnpm npm yarn cargo bundle rails go make xcodebuild swift kubectl docker podman aws gcloud az ls cd cat echo env export open node java ruby perl bash zsh fish pwsh sh",a," ")
 s=tolower(s); for(i=1;i<=n;i++) if(index(s,a[i])==1) return 1
 return 0
}
function command(s) { s=trim(s); return s!="" && (index(s,"[[ ")==1 || (s !~ /\.$/ && s ~ /^(sudo[[:space:]]+)?[A-Za-z0-9.\/~_-]+([[:space:]]|$)/)) }
function prompt(s, t,token) {
 t=trim(s); if(t !~ /^[#$]/) return 0
 t=trim(substr(t,2)); token=t; sub(/ .*/,"",token)
 return t!="" && t !~ /[.?!]$/ && (t ~ /[-.\/~$ 0-9]/ || known(token)) && command(t)
}
function pathquote(s, t,token) {
 t=trim(s); if(t=="" || index(t,"\n")) return s
 if((substr(t,1,1)=="\"" && substr(t,length(t),1)=="\"") || (substr(t,1,1)==sprintf("%c",39) && substr(t,length(t),1)==sprintf("%c",39))) return s
 token=t; sub(/[[:space:]].*/,"",token)
 if(index(t,"://") || !index(token,"/") || !index(t," ") || t ~ /[[:space:]]-[A-Za-z]/) return s
 gsub(/"/,"\\\"",t); return "\"" t "\""
}
function punctuation(s) { return s ~ /@|(^|[[:space:]])--[A-Za-z0-9][A-Za-z0-9_-]*|(^|[[:space:]])-[A-Za-z]([[:space:]]|$)|[A-Za-z_][A-Za-z0-9_]*=|(^|[[:space:]])(\.\/|~\/|\/|\.[A-Za-z0-9_-]+)|[<>]/ }
function flatten(s, p,left,right) {
 # Equivalent joins to Trimmy, expressed without lookbehind/capture replacements.
 while(match(s,/[A-Za-z0-9._~-]-[[:space:]]*\n[[:space:]]*[A-Za-z0-9._~-]/)) {
   p=substr(s,RSTART,RLENGTH); left=substr(p,1,2); right=substr(p,length(p),1)
   s=substr(s,1,RSTART-1) left right substr(s,RSTART+RLENGTH)
 }
 while(match(s,/[A-Z0-9_.-][ \t\r]*\n[ \t\r]*[A-Z0-9_.-]/)) {
   p=substr(s,RSTART,RLENGTH)
   s=substr(s,1,RSTART-1) substr(p,1,1) substr(p,length(p),1) substr(s,RSTART+RLENGTH)
 }
 while(match(s,/[\/~][[:space:]]*\n[[:space:]]*[A-Za-z0-9._-]/)) {
   p=substr(s,RSTART,RLENGTH)
   s=substr(s,1,RSTART-1) substr(p,1,1) substr(p,length(p),1) substr(s,RSTART+RLENGTH)
 }
 gsub(/\\[ \t\r]*\n/," ",s); gsub(/[[:space:]]+/," ",s); return trim(s)
}
function transform(s, a,n,i,t,nonempty,lead,tail,threshold,stripcount,rebuilt,lower,urls,temp,explicit,lists,commands,strong,prefix,score,allcmd,cont,saw,box) {
 box="[│┃╎╏┆┇┊┋╽╿￨｜]"
 if(s ~ box) {
   gsub(/│ │/," ",s); n=split(s,a,"\n"); nonempty=lead=tail=0
   for(i=1;i<=n;i++) if(trim(a[i])!="") {
     nonempty++; if(a[i] ~ ("^[[:space:]]*" box "+[[:space:]]?")) lead++
     if(a[i] ~ ("[[:space:]]*" box "+[[:space:]]*$")) tail++
   }
   threshold=int(nonempty/2)+1; rebuilt=""
   for(i=1;i<=n;i++) {
     if(lead>=threshold) sub("^[[:space:]]*" box "+[[:space:]]?","",a[i])
     if(tail>=threshold) sub("[[:space:]]*" box "+[[:space:]]*$","",a[i])
     rebuilt=rebuilt (i>1?"\n":"") a[i]
   }
   s=rebuilt
   # Path separators next to a box join directly; other boxes become spaces.
   while(match(s,"[:/][[:space:]]*" box "+[[:space:]]*[A-Za-z0-9]")) {
     t=substr(s,RSTART,RLENGTH)
     s=substr(s,1,RSTART-1) substr(t,1,1) substr(t,length(t),1) substr(s,RSTART+RLENGTH)
   }
   gsub("[[:space:]]*" box "+[[:space:]]*"," ",s)
   gsub(/  +/," ",s); s=trim(s)
 }
 n=split(s,a,"\n"); nonempty=stripcount=0
 for(i=1;i<=n;i++) { if(trim(a[i])!="") nonempty++; if(prompt(a[i])) stripcount++ }
 if(stripcount>=int(nonempty/2)+1) {
   s=""; for(i=1;i<=n;i++) {
     if(prompt(a[i])) sub(/[#$][[:space:]]*/,"",a[i])
     s=s (i>1?"\n":"") a[i]
   }
 }
 t=trim(s); lower=tolower(t); temp=lower
 urls=gsub(/https:\/\//,"",temp); urls+=gsub(/http:\/\//,"",temp)
 if(urls==1 && lower ~ /^https?:\/\//) { gsub(/[[:space:]]+/,"",t); s=t }
 s=pathquote(s)
 if(!index(s,"\n")) return s
 n=split(s,a,"\n"); explicit=0; nonempty=lists=commands=prefix=0; allcmd=1
 for(i=1;i<=n;i++) {
   t=trim(a[i]); if(a[i] ~ /[\\|&;][[:space:]]*$/ || a[i] ~ /^[[:space:]]*[|&][|&]?[[:space:]]+[^[:space:]]/) explicit=1
   if(!command(a[i])) allcmd=0
   if(t!="") {
     nonempty++; if(command(t)) commands++
     if(t ~ /^[-*•][[:space:]]+[^[:space:]]|^[0-9]+[.)][[:space:]]+[^[:space:]]/ || (t ~ /^[A-Za-z0-9]+$/ && length(t)>=4)) lists++
     temp=t; sub(/ .*/,"",temp); if(known(temp)) prefix=1
   }
 }
 if((n>4 && !explicit) || lists>=int(nonempty/2)+1 || (n>30 && !explicit) || (!explicit && commands==nonempty && nonempty>=3)) return s
 strong=(index(s,"\\\n") || s ~ /[|&]/ || s ~ /(^|\n)[[:space:]]*(\n|$)/ || s ~ /[A-Za-z0-9._~-]+\/[A-Za-z0-9._~-]+/)
 if(!strong && !prefix && !punctuation(s)) return s
 if(!strong && (s ~ /[{}]/ || tolower(s) ~ /begin/) && s ~ /(^|\n)[[:space:]]*(import|package|namespace|using|template|class|struct|enum|extension|protocol|interface|func|def|fn|let|var|public|private|internal|open|protected|if|for|while)([[:space:]]|$)/) return s
 score=0
 if(index(s,"\\\n")) score++
 if(s ~ /[|&]/) score++
 if(s ~ /(^|\n)[[:space:]]*(\n|$)/) score++
 cont=1; saw=0; temp=0
 for(i=1;i<=n;i++) if(trim(a[i])!="") {
   if(!temp++) { if(!command(a[i])) cont=0; continue }
   if(a[i] ~ /^[[:space:]]/) saw=1
   else if(trim(a[i]) !~ /^(\||&& |;|>|2>|<|-)/) cont=0
 }
 if(nonempty>=2 && cont && saw) score++
 if(allcmd) score++
 if(s ~ /(^|\n)[[:space:]]*(sudo[[:space:]]+)?[A-Za-z0-9.\/~_-]+/) score++
 if(s ~ /[A-Za-z0-9._~-]+\/[A-Za-z0-9._~-]+/) score++
 return score>=2 ? flatten(s) : s
}
{ text=text $0 "\n" }
END { printf "%s", transform(text) }
'
}

extract() {
awk -v dir="$2" '
function fence(s, t,c,n) {
 t=s; sub(/^ */,"",t); indent=length(s)-length(t)
 if(indent>3) return 0
 c=substr(t,1,1); if(c!="`" && c!="~") return 0
 n=0; while(substr(t,n+1,1)==c) n++
 if(n<3) return 0
 char=c; size=n; label=substr(t,n+1); sub(/^[ \t]+/,"",label); sub(/[ \t]+$/,"",label)
 return 1
}
function save( a,n,i,p,common,s,file) {
 n=split(body,a,"\n"); common=-1
 for(i=1;i<n;i++) if(a[i] ~ /[^ \t]/) {
   s=a[i]; sub(/^[ \t]*/,"",s); p=substr(a[i],1,length(a[i])-length(s))
   if(common==-1) common=p
   else { while(index(p,common)!=1 && length(common)) common=substr(common,1,length(common)-1) }
 }
 if(common==-1) return
 count++; file=dir "/" count
 for(i=1;i<n;i++) print substr(a[i],length(common)+1) > file
 close(file); print language > (dir "/" count ".label"); close(dir "/" count ".label")
}
{
 line=$0; sub(/[ \t\r]+$/,"",line); isf=fence(line)
 if(!active) {
   if(isf && !(char=="`" && index(label,"`"))) { active=1; mark=char; width=size; pad=indent; language=label; body="" }
 } else if(isf && char==mark && size>=width && label=="") { save(); active=0 }
 else if(isf && char==mark && size>=width && label!="" && !(char=="`" && index(label,"`"))) {
   mark=char; width=size; pad=indent; language=label; body=""
 } else {
   for(i=0;i<pad && substr(line,1,1)==" ";i++) line=substr(line,2)
   body=body line "\n"
 }
}
END { print count+0 > (dir "/count") }
' "$1"
}

case ${1:-} in
--clean) clean; exit ;;
--extract) extract "$2" "$3"; exit ;;
--pick)
  dir=$2; client=$3
  IFS= read -r count < "$dir/count"
  if [ "$count" -eq 0 ]; then tmux display-message -c "$client" 'No complete fenced blocks on the visible screen'; exit; fi
  rows=(); owners=(); starts=(); ends=()
  for ((i=1;i<=count;i++)); do
    IFS= read -r label < "$dir/$i.label"
    label=${label//[$'\001'-$'\037'$'\177']/ }
    starts[i]=${#rows[@]}; rows+=("[$i] ${label:-text}"); owners+=("$i")
    while IFS= read -r line; do
      # Display only: strip terminal control characters, never interpret content.
      line=${line//[$'\001'-$'\037'$'\177']/ }
      rows+=("  $line"); owners+=("$i")
    done < "$dir/$i"
    ends[i]=${#rows[@]}; rows+=(""); owners+=(0)
  done
  saved=$(stty -g)
  restore() { stty "$saved"; printf '\033[0m\033[?25h\033[?7h\033[?1049l'; }
  trap restore EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stty -echo -icanon min 1 time 0
  printf '\033[?1049h\033[?25l\033[?7l'
  selected=1; top=0
  while :; do
    read -r height width < <(stty size)
    available=$((height-2)); [ "$available" -gt 0 ] || available=1
    start=${starts[selected]}; end=${ends[selected]}
    if ((start<top || end-start>available)); then top=$start
    elif ((end>top+available)); then top=$((end-available)); fi
    maximum=$((${#rows[@]}-available)); ((maximum>=0)) || maximum=0
    ((top<=maximum)) || top=$maximum
    printf '\033[H\033[0m\033[2K%s' "${header:-j/k: select | Enter: clean | r: raw | Esc/q: cancel}"
    for ((r=0;r<available;r++)); do
      at=$((top+r)); printf '\033[%d;1H\033[0m\033[2K' "$((r+2))"
      if ((at<${#rows[@]})); then
        [[ ${owners[at]} == "$selected" ]] && printf '\033[7m'
        printf '%s' "${rows[at]:0:width-1}"
      fi
    done
    printf '\033[%d;1H\033[0m\033[2KBlock %d/%d — original preview' "$height" "$selected" "$count"
    IFS= read -r -n 1 key || break
    if [[ $key == $'\033' ]]; then
      # macOS Bash 3.2 has no fractional read timeout; use the tty timer.
      stty min 0 time 1
      # Bash retries zero-byte tty reads; dd respects the tty timeout.
      next=$(dd bs=1 count=1 2>/dev/null)
      if [[ $next == '[' || $next == O ]]; then
        next=$(dd bs=1 count=1 2>/dev/null)
        stty min 1 time 0
        case $next in A) key=k;; B) key=j;; *) continue;; esac
      else break; fi
    fi
    case $key in
      q) break;;
      j) selected=$((selected%count+1));;
      k) selected=$(((selected+count-2)%count+1));;
      ''|r)
        if [[ $key == r ]]; then cp "$dir/$selected" "$dir/result"
        else clean < "$dir/$selected" > "$dir/result"; fi
        tmux load-buffer -w -t "$client" "$dir/result"
        tmux display-message -c "$client" 'Copied block'
        break;;
    esac
  done
  exit;;
esac

pane=${1:?pane required}; client=${2:?client required}
script=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")
dir=$(mktemp -d "${TMPDIR:-/tmp}/tmux-copy-shell.XXXXXXXX")
trap 'rm -rf -- "$dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
viewport=$(tmux display-message -p -t "$pane" '#{pane_height} #{scroll_position}')
height=${viewport%% *}; scroll=${viewport#* }; scroll=${scroll:-0}
tmux capture-pane -p -J -S "$((-scroll))" -E "$((height-scroll-1))" -t "$pane" > "$dir/screen"
extract "$dir/screen" "$dir"
tmux display-popup -E -c "$client" -w '90%' -h '80%' -T 'Copy visible code block' \
  -e "COPY_SHELL_SCRIPT=$script" -e "COPY_SHELL_DIR=$dir" -e "COPY_SHELL_CLIENT=$client" \
  'exec bash "$COPY_SHELL_SCRIPT" --pick "$COPY_SHELL_DIR" "$COPY_SHELL_CLIENT"'
