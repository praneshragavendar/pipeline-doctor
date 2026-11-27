// Jenkins pipeline for PipelineDoctor itself.
// Works on Linux agents (sh). For Windows agents replace `sh` with `bat`/`powershell`.
pipeline {
    agent any

    options {
        timeout(time: 15, unit: 'MINUTES')
        timestamps()
    }

    environment {
        VENV = "${WORKSPACE}/.venv"
        PIPELINEDOCTOR_LOG_DIR = "${WORKSPACE}/sample_logs"
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    python3 -m venv "$VENV"
                    "$VENV/bin/pip" install --upgrade pip
                    "$VENV/bin/pip" install -e ".[dev]"
                '''
            }
        }

        stage('Lint rules & knowledge base') {
            // Linter gate: a broken rule or incident file fails the build here.
            steps {
                sh '"$VENV/bin/pipelinedoctor" lint --strict'
            }
        }

        stage('Unit tests') {
            steps {
                sh '"$VENV/bin/pytest" -v --junitxml=reports/unit-tests.xml'
            }
        }

        stage('Smoke test on sample logs') {
            steps {
                sh '''
                    mkdir -p reports
                    ./scripts/diagnose.sh "$PIPELINEDOCTOR_LOG_DIR"/*.log > reports/diagnoses.txt
                    "$VENV/bin/pipelinedoctor" analyze "$PIPELINEDOCTOR_LOG_DIR"/*.log --no-llm \
                        --format junit > reports/sample-diagnoses.xml
                '''
            }
        }
    }

    post {
        always {
            // Jenkins shows both unit test results and diagnoses in its Test Results view.
            junit allowEmptyResults: true, testResults: 'reports/unit-tests.xml'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
    }
}
