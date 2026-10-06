{{- define "reviewbot.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "reviewbot.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "reviewbot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "reviewbot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels for one component: include "reviewbot.selectorLabels" (list . "api") */}}
{{- define "reviewbot.selectorLabels" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ include "reviewbot.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "reviewbot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "reviewbot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "reviewbot.apiImage" -}}
{{- printf "%s:%s" .Values.image.api.repository (default .Chart.AppVersion .Values.image.api.tag) -}}
{{- end -}}

{{- define "reviewbot.webImage" -}}
{{- printf "%s:%s" .Values.image.web.repository (default .Chart.AppVersion .Values.image.web.tag) -}}
{{- end -}}

{{- define "reviewbot.secretName" -}}
{{- default (printf "%s-secrets" (include "reviewbot.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "reviewbot.publicUrl" -}}
{{- required "publicUrl is required, e.g. https://reviewbot.example.com" .Values.publicUrl | trimSuffix "/" -}}
{{- end -}}

{{/* Environment shared by the API, worker, beat and migration pods. */}}
{{- define "reviewbot.backendEnv" -}}
envFrom:
  - configMapRef:
      name: {{ include "reviewbot.fullname" . }}-config
  - secretRef:
      name: {{ include "reviewbot.secretName" . }}
{{- end -}}

{{- define "reviewbot.podScheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Checksums so pods restart when configuration changes. */}}
{{- define "reviewbot.configChecksums" -}}
checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- if not .Values.secrets.existingSecret }}
checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
{{- end -}}

{{/* Init container: waits until the migration Job has applied every migration. */}}
{{- define "reviewbot.waitForMigrations" -}}
{{- if .Values.migrations.enabled }}
initContainers:
  - name: wait-for-migrations
    image: {{ include "reviewbot.apiImage" . }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command: ["sh", "-c", "until python manage.py migrate --check >/dev/null 2>&1; do echo waiting for migrations; sleep 5; done"]
    {{- include "reviewbot.backendEnv" . | nindent 4 }}
    securityContext:
      {{- toYaml .Values.securityContext | nindent 6 }}
    resources:
      requests: {cpu: 10m, memory: 128Mi}
      limits: {memory: 256Mi}
    volumeMounts:
      - {name: tmp, mountPath: /tmp}
{{- end }}
{{- end -}}

{{/* HorizontalPodAutoscaler for a component: include "reviewbot.hpa" (list . "api" .Values.api) */}}
{{- define "reviewbot.hpa" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{- $values := index . 2 -}}
{{- if $values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "reviewbot.fullname" $root }}-{{ $component }}
  labels:
    {{- include "reviewbot.labels" $root | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "reviewbot.fullname" $root }}-{{ $component }}
  minReplicas: {{ $values.autoscaling.minReplicas }}
  maxReplicas: {{ $values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ $values.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
{{- end -}}

{{/* PodDisruptionBudget for a component. */}}
{{- define "reviewbot.pdb" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{- $values := index . 2 -}}
{{- if $values.podDisruptionBudget.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "reviewbot.fullname" $root }}-{{ $component }}
  labels:
    {{- include "reviewbot.labels" $root | nindent 4 }}
spec:
  minAvailable: {{ $values.podDisruptionBudget.minAvailable }}
  selector:
    matchLabels:
      {{- include "reviewbot.selectorLabels" (list $root $component) | nindent 6 }}
{{- end }}
{{- end -}}
