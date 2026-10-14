// Copied and hash-attested in a managed generation; never a Node env preload.
// QMD 2.5.3 exposes ensureLlama(false): every native lookup uses build:never
// and skipDownload:true, including packaged GPU candidates and CPU fallback.
import {pathToFileURL} from 'node:url';
const cli=__QMD_CLI_JSON__;
delete process.env.QMD_FORCE_CPU;
delete process.env.QMD_LLAMA_GPU;
if(process.platform==='darwin')process.env.GGML_METAL_NO_RESIDENCY='1';
if(process.argv[2]==='mcp') {
 process.env.LLAMA_LOG_LEVEL='error';process.env.GGML_LOG_LEVEL='error';
 process.env.GGML_BACKEND_SILENT='1';
}
const {LlamaCpp}=await import(new URL('../llm.js',pathToFileURL(cli)));
if(typeof LlamaCpp?.prototype?.ensureLlama!=='function')throw new Error('qmd_no_build_contract_missing');
const ensure=LlamaCpp.prototype.ensureLlama;
LlamaCpp.prototype.ensureLlama=async function() {
 const first=!this.llama;
 const llama=await ensure.call(this,false);
 if(first)process.stderr.write(JSON.stringify({schema:'qmd-native-no-build-v1',
   gpu:llama.gpu,build:'never',skipDownload:true})+'\n');
 return llama;
};
// Preserve QMD's own parsing, configuration, DB and main-module guard.
process.argv[1]=cli;
await import(pathToFileURL(cli));
