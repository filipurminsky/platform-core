{{- define "vllm.fullname" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "vllm.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "vllm.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "vllm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vllm.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Resolve the active model name — devMode overrides the primary model */}}
{{- define "vllm.modelName" -}}
{{- if .Values.devMode.enabled -}}
{{ .Values.devMode.model }}
{{- else -}}
{{ .Values.model.name }}
{{- end }}
{{- end }}

{{/* Resolve resources based on devMode */}}
{{- define "vllm.resources" -}}
{{- if .Values.devMode.enabled -}}
{{ toYaml .Values.devMode.resources }}
{{- else -}}
{{ toYaml .Values.resources }}
{{- end }}
{{- end }}
