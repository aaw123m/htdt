#include "mfem.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{

constexpr double kPi = 3.141592653589793238462643383279502884;

struct SampleResult
{
   double frequency_hz = 0.0;
   double pressure_real_pa = 0.0;
   double pressure_imag_pa = 0.0;
   int iterations = 0;
   double relative_residual = 0.0;
};

struct CaseResult
{
   std::string case_id;
   int order = 0;
   int ndofs = 0;
   int elements = 0;
   int boundary_faces = 0;
   int total_faces = 0;
   int internal_faces = 0;
   std::vector<int> element_attributes;
   double assemble_s = 0.0;
   double solve_s = 0.0;
   int max_iterations = 0;
   double max_relative_residual = 0.0;
   std::vector<SampleResult> samples;
};

mfem::Mesh BuildRoom(bool portal_regions)
{
   // Exact common compiled representation for:
   // - wave-rectangular-convergence-v1 peer room
   // - wave-portal-split-room-v1 two regions joined by full-height Portal.
   //
   // Both use the same conforming x=3 m mesh partition. The only semantic
   // difference is element region attributes. No boundary element is created
   // on the shared x=3 m face, so H1 pressure continuity and weak normal-flux
   // continuity are not replaced by an artificial wall/impedance.
   mfem::Mesh mesh(3, 12, 2, 0, 3);

   const double xs[] = {0.0, 3.0, 6.0};
   const double ys[] = {0.0, 4.0};
   const double zs[] = {0.0, 2.5};

   for (double z : zs)
   {
      for (double y : ys)
      {
         for (double x : xs)
         {
            mesh.AddVertex(x, y, z);
         }
      }
   }

   auto vid = [](int ix, int iy, int iz)
   {
      return iz * 6 + iy * 3 + ix;
   };

   auto add_cell = [&](int ix, int attribute)
   {
      mesh.AddHex(
         vid(ix,     0, 0),
         vid(ix + 1, 0, 0),
         vid(ix + 1, 1, 0),
         vid(ix,     1, 0),
         vid(ix,     0, 1),
         vid(ix + 1, 0, 1),
         vid(ix + 1, 1, 1),
         vid(ix,     1, 1),
         attribute);
   };

   add_cell(0, 1);
   add_cell(1, portal_regions ? 2 : 1);
   mesh.FinalizeHexMesh(1, 0, true);
   return mesh;
}

CaseResult SolveCase(
   bool portal_regions,
   int order,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   using clock = std::chrono::steady_clock;

   mfem::Mesh mesh = BuildRoom(portal_regions);
   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);

   const auto assemble_started = clock::now();

   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator);
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::DeltaCoefficient source_delta(source_x, source_y, source_z, 1.0);
   mfem::LinearForm source_functional(&fes);
   source_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(source_delta));
   source_functional.Assemble();

   mfem::DeltaCoefficient receiver_delta(receiver_x, receiver_y, receiver_z, 1.0);
   mfem::LinearForm receiver_functional(&fes);
   receiver_functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(receiver_delta));
   receiver_functional.Assemble();

   const auto assemble_finished = clock::now();

   mfem::SparseMatrix &K = stiffness.SpMat();
   mfem::SparseMatrix &M = mass.SpMat();

   mfem::Vector solution(fes.GetTrueVSize());
   solution = 0.0;
   bool have_initial_guess = false;

   CaseResult result;
   result.case_id = portal_regions ? "portal-two-region" : "peer-one-region";
   result.order = order;
   result.ndofs = fes.GetTrueVSize();
   result.elements = mesh.GetNE();
   result.boundary_faces = mesh.GetNBE();
   result.total_faces = mesh.GetNumFaces();
   result.internal_faces = result.total_faces - result.boundary_faces;
   result.assemble_s =
      std::chrono::duration<double>(assemble_finished - assemble_started).count();

   std::set<int> attributes;
   for (int element = 0; element < mesh.GetNE(); ++element)
   {
      attributes.insert(mesh.GetElement(element)->GetAttribute());
   }
   result.element_attributes.assign(attributes.begin(), attributes.end());

   const auto solve_started = clock::now();
   const int frequency_count = static_cast<int>(
      std::llround((frequency_stop_hz - frequency_start_hz) / frequency_step_hz)) + 1;

   for (int index = 0; index < frequency_count; ++index)
   {
      const double frequency_hz = frequency_start_hz + index * frequency_step_hz;
      const double omega = 2.0 * kPi * frequency_hz;
      const double k = omega / sound_speed_m_s;

      // exp(-i*omega*t):
      //   div(grad p) + k^2 p = i*omega*rho*Q*delta
      // with rigid natural-Neumann exterior boundary:
      //   (grad p,grad v) - k^2(p,v) = -i*omega*rho*Q v(xs).
      std::unique_ptr<mfem::SparseMatrix> A(mfem::Add(1.0, K, -k * k, M));
      std::unique_ptr<mfem::SparseMatrix> P(mfem::Add(1.0, K,  k * k, M));

      mfem::Vector rhs(source_functional);
      rhs *= -omega * density_kg_m3 * source_amplitude_m3_s;

      mfem::DSmoother preconditioner(*P);
      mfem::MINRESSolver minres;
      minres.iterative_mode = have_initial_guess;
      minres.SetOperator(*A);
      minres.SetPreconditioner(preconditioner);
      minres.SetMaxIter(8000);
      minres.SetRelTol(1e-10);
      minres.SetAbsTol(0.0);
      minres.SetPrintLevel(0);

      if (!have_initial_guess)
      {
         solution = 0.0;
      }

      minres.Mult(rhs, solution);
      if (!minres.GetConverged())
      {
         throw std::runtime_error(
            "MINRES did not converge for " + result.case_id
            + " frequency_hz=" + std::to_string(frequency_hz)
            + " iterations=" + std::to_string(minres.GetNumIterations())
            + " final_norm=" + std::to_string(minres.GetFinalNorm()));
      }
      have_initial_guess = true;

      mfem::Vector residual(rhs.Size());
      A->Mult(solution, residual);
      residual -= rhs;
      const double rhs_norm = rhs.Norml2();
      const double relative_residual =
         residual.Norml2() / std::max(rhs_norm, 1e-30);

      SampleResult sample;
      sample.frequency_hz = frequency_hz;
      sample.pressure_real_pa = 0.0;
      sample.pressure_imag_pa = receiver_functional * solution;
      sample.iterations = minres.GetNumIterations();
      sample.relative_residual = relative_residual;
      result.samples.push_back(sample);
      result.max_iterations = std::max(result.max_iterations, sample.iterations);
      result.max_relative_residual =
         std::max(result.max_relative_residual, sample.relative_residual);
   }

   const auto solve_finished = clock::now();
   result.solve_s =
      std::chrono::duration<double>(solve_finished - solve_started).count();
   return result;
}

void WriteCase(std::ofstream &os, const CaseResult &value, bool trailing_comma)
{
   os << "    {\n";
   os << "      \"case_id\": \"" << value.case_id << "\",\n";
   os << "      \"order\": " << value.order << ",\n";
   os << "      \"elements\": " << value.elements << ",\n";
   os << "      \"ndofs\": " << value.ndofs << ",\n";
   os << "      \"boundary_faces\": " << value.boundary_faces << ",\n";
   os << "      \"total_faces\": " << value.total_faces << ",\n";
   os << "      \"internal_faces\": " << value.internal_faces << ",\n";
   os << "      \"element_attributes\": [";
   for (size_t i = 0; i < value.element_attributes.size(); ++i)
   {
      if (i) { os << ", "; }
      os << value.element_attributes[i];
   }
   os << "],\n";
   os << "      \"assemble_s\": " << value.assemble_s << ",\n";
   os << "      \"solve_s\": " << value.solve_s << ",\n";
   os << "      \"max_iterations\": " << value.max_iterations << ",\n";
   os << "      \"max_relative_residual\": " << value.max_relative_residual << ",\n";
   os << "      \"samples\": [\n";
   for (size_t i = 0; i < value.samples.size(); ++i)
   {
      const auto &sample = value.samples[i];
      os << "        {"
         << "\"frequency_hz\":" << sample.frequency_hz << ","
         << "\"pressure_real_pa\":" << sample.pressure_real_pa << ","
         << "\"pressure_imag_pa\":" << sample.pressure_imag_pa << ","
         << "\"iterations\":" << sample.iterations << ","
         << "\"relative_residual\":" << sample.relative_residual
         << "}" << (i + 1 == value.samples.size() ? "" : ",") << "\n";
   }
   os << "      ]\n";
   os << "    }" << (trailing_comma ? "," : "") << "\n";
}

void WriteJson(
   const std::string &path,
   const CaseResult &peer,
   const CaseResult &portal,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   std::ofstream os(path, std::ios::binary);
   if (!os) { throw std::runtime_error("cannot open output file: " + path); }

   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-portal-transfer-raw-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"compiled_geometry\": \"6x4x2.5-two-conforming-hexes-shared-x3-interface\",\n";
   os << "  \"portal_interface_boundary_condition\": \"none-internal-shared-face\",\n";
   os << "  \"exterior_boundary_model\": \"natural-neumann-rigid\",\n";
   os << "  \"fourier_sign\": \"exp(-i*omega*t)\",\n";
   os << "  \"linear_system\": \"K-k^2M\",\n";
   os << "  \"preconditioner\": \"diagonal-jacobi-on-K+k^2M\",\n";
   os << "  \"source_rhs\": \"-i*omega*rho*Q*delta\",\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": [" << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": [" << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_amplitude_m3_s\": " << source_amplitude_m3_s << ",\n";
   os << "  \"frequency_grid_hz\": {"
      << "\"start\":" << frequency_start_hz << ","
      << "\"stop\":" << frequency_stop_hz << ","
      << "\"step\":" << frequency_step_hz << "},\n";
   os << "  \"cases\": [\n";
   WriteCase(os, peer, true);
   WriteCase(os, portal, false);
   os << "  ]\n";
   os << "}\n";
}

} // namespace

int ProbeMain(int argc, char *argv[])
{
   double density_kg_m3 = 1.2;
   double sound_speed_m_s = 343.0;
   double source_x = 1.0, source_y = 1.0, source_z = 1.0;
   double receiver_x = 5.0, receiver_y = 3.0, receiver_z = 1.0;
   double source_amplitude_m3_s = 1.0;
   double frequency_start_hz = 20.0;
   double frequency_stop_hz = 300.0;
   double frequency_step_hz = 1.0;
   int order = 3;
   std::string output;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto value = [&](const char *name)
      {
         if (i + 1 >= argc)
         {
            throw std::runtime_error(std::string("missing value for ") + name);
         }
         return std::string(argv[++i]);
      };

      if (arg == "--density") { density_kg_m3 = std::stod(value("--density")); }
      else if (arg == "--sound-speed") { sound_speed_m_s = std::stod(value("--sound-speed")); }
      else if (arg == "--source-x") { source_x = std::stod(value("--source-x")); }
      else if (arg == "--source-y") { source_y = std::stod(value("--source-y")); }
      else if (arg == "--source-z") { source_z = std::stod(value("--source-z")); }
      else if (arg == "--receiver-x") { receiver_x = std::stod(value("--receiver-x")); }
      else if (arg == "--receiver-y") { receiver_y = std::stod(value("--receiver-y")); }
      else if (arg == "--receiver-z") { receiver_z = std::stod(value("--receiver-z")); }
      else if (arg == "--source-amplitude") { source_amplitude_m3_s = std::stod(value("--source-amplitude")); }
      else if (arg == "--frequency-start") { frequency_start_hz = std::stod(value("--frequency-start")); }
      else if (arg == "--frequency-stop") { frequency_stop_hz = std::stod(value("--frequency-stop")); }
      else if (arg == "--frequency-step") { frequency_step_hz = std::stod(value("--frequency-step")); }
      else if (arg == "--order") { order = std::stoi(value("--order")); }
      else if (arg == "--output") { output = value("--output"); }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (output.empty()) { throw std::runtime_error("--output is required"); }
   if (!(density_kg_m3 > 0.0 && sound_speed_m_s > 0.0 && source_amplitude_m3_s > 0.0))
   {
      throw std::runtime_error("density, sound speed and source amplitude must be positive");
   }
   if (!(frequency_start_hz > 0.0 && frequency_stop_hz >= frequency_start_hz
         && frequency_step_hz > 0.0))
   {
      throw std::runtime_error("invalid frequency grid");
   }
   if (order < 1 || order > 6)
   {
      throw std::runtime_error("invalid polynomial order");
   }

   const CaseResult peer = SolveCase(
      false, order, density_kg_m3, sound_speed_m_s,
      source_x, source_y, source_z,
      receiver_x, receiver_y, receiver_z,
      source_amplitude_m3_s,
      frequency_start_hz, frequency_stop_hz, frequency_step_hz);
   const CaseResult portal = SolveCase(
      true, order, density_kg_m3, sound_speed_m_s,
      source_x, source_y, source_z,
      receiver_x, receiver_y, receiver_z,
      source_amplitude_m3_s,
      frequency_start_hz, frequency_stop_hz, frequency_step_hz);

   WriteJson(
      output, peer, portal,
      density_kg_m3, sound_speed_m_s,
      source_x, source_y, source_z,
      receiver_x, receiver_y, receiver_z,
      source_amplitude_m3_s,
      frequency_start_hz, frequency_stop_hz, frequency_step_hz);
   return 0;
}

int main(int argc, char *argv[])
{
   try
   {
      return ProbeMain(argc, argv);
   }
   catch (const std::exception &exc)
   {
      std::cerr << "R100B_MFEM_PORTAL_FATAL: " << exc.what() << std::endl;
      return 2;
   }
   catch (...)
   {
      std::cerr << "R100B_MFEM_PORTAL_FATAL: unknown exception" << std::endl;
      return 3;
   }
}
