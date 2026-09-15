// Peretashchi syuda CR2.js - tselevoy fayl dlya drag-and-drop.
//
// Zachem on nuzhen: Provodnik zapuskaet .bat cherez cmd.exe, a cmd razbiraet
// komandnuyu stroku do pervoy stroki .bat. Imya papki s simvolom & ili ^
// Provodnik NE beret v kavychki (kavychki dobavlyayutsya tolko pri probele),
// poetomu put teryaetsya bezvozvratno - vnutri .bat ego uzhe ne vosstanovit.
//
// wscript.exe poluchaet argumenty cherez CommandLineToArgvW, gde & i ^ -
// obychnye simvoly. Zdes my prosto beryem ih v kavychki i peredaem v .bat.
var sh  = WScript.CreateObject("WScript.Shell");
var fso = WScript.CreateObject("Scripting.FileSystemObject");
var dir = fso.GetParentFolderName(WScript.ScriptFullName);
var bat = dir + "\\Peretashchi syuda CR2.bat";
if (!fso.FileExists(bat)) { bat = dir + "\\\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438 \u0441\u044e\u0434\u0430 CR2.bat"; }
if (!fso.FileExists(bat)) {
    sh.Popup("Ne nayden fayl 'Peretashchi syuda CR2.bat' ryadom s etim skriptom.",
             0, "Konverter CR2", 16);
    WScript.Quit(2);
}
var args = "";
for (var i = 0; i < WScript.Arguments.length; i++) {
    args += ' "' + WScript.Arguments(i) + '"';
}
if (args === "") {
    sh.Popup("Peretashchite na etot fayl papku ili fayly .CR2.",
             0, "Konverter CR2", 64);
    WScript.Quit(2);
}
WScript.Quit(sh.Run('cmd /c ""' + bat + '"' + args + '"', 1, true));
